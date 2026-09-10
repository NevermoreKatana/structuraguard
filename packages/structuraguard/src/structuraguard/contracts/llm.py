"""Provider-neutral capabilities, prompt identity и безопасная история попыток."""

from datetime import timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import FrozenContract
from .common import (
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    ParserIdentifierStr,
    PositiveInt,
    SchemaVersionStr,
    UtcDateTime,
    VersionStr,
)


class LLMExecutionEnvironment(StrEnum):
    """Доверенная классификация deployment; unknown не означает local."""

    UNKNOWN = "unknown"
    LOCAL = "local"
    CLOUD = "cloud"
    DISABLED = "disabled"


class LLMErrorCode(StrEnum):
    """Нормализованные ошибки provider boundary без backend-specific details."""

    TIMEOUT = "LLM_TIMEOUT"
    RATE_LIMIT = "LLM_RATE_LIMIT"
    UNAVAILABLE = "LLM_UNAVAILABLE"
    INVALID_RESPONSE = "LLM_INVALID_RESPONSE"
    REQUEST_INVALID = "LLM_REQUEST_INVALID"
    CAPABILITY_MISMATCH = "LLM_CAPABILITY_MISMATCH"
    CONTEXT_LIMIT = "LLM_CONTEXT_LIMIT"
    POLICY_DENIED = "LLM_POLICY_DENIED"
    SCRIPT_EXHAUSTED = "LLM_SCRIPT_EXHAUSTED"
    SCRIPT_MISMATCH = "LLM_SCRIPT_MISMATCH"
    AUTHENTICATION_FAILED = "LLM_AUTHENTICATION_FAILED"
    SCHEMA_VIOLATION = "LLM_SCHEMA_VIOLATION"
    BUDGET_EXCEEDED = "LLM_BUDGET_EXCEEDED"
    UNKNOWN_SOURCE_REFERENCE = "LLM_UNKNOWN_SOURCE_REFERENCE"
    UNSAFE_CONTENT = "LLM_UNSAFE_CONTENT"


class LLMPrompt(FrozenContract):
    """Версия и hash trusted prompt без текста инструкций или source data."""

    prompt_id: ParserIdentifierStr
    version: SchemaVersionStr
    fingerprint: FingerprintStr


class LLMPlanProvenance(FrozenContract):
    """Происхождение semantic plan без raw prompt/response и self-confidence."""

    prompt: LLMPrompt
    request_fingerprint: FingerprintStr
    generation_fingerprint: FingerprintStr
    response_schema_fingerprint: FingerprintStr
    provider_id: IdentifierStr
    provider_version: VersionStr
    model_id: IdentifierStr
    score_policy: Literal["candidate_min_v1"] = "candidate_min_v1"


class LLMCallRecord(FrozenContract):
    """Bounded diagnostics: только hashes, счётчики и управляемое время.

    Request/provider fingerprints связывают исходные DTO без копирования даже
    их caller-controlled идентификаторов. Raw payload, response и headers здесь
    отсутствуют. None usage означает неизвестное значение, а не ноль tokens.
    """

    attempt: PositiveInt
    request_fingerprint: FingerprintStr
    capabilities_fingerprint: FingerprintStr
    prompt: LLMPrompt
    outcome: Literal["success", "failed", "cancelled"]
    error_code: LLMErrorCode | None = None
    generation_fingerprint: FingerprintStr | None = None
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    elapsed_ms: Annotated[NonNegativeInt, Field(le=300_000)]
    started_at: UtcDateTime
    finished_at: UtcDateTime
    provider_id: IdentifierStr | None = None
    provider_version: VersionStr | None = None
    model_id: IdentifierStr | None = None
    fallback_reason: LLMErrorCode | None = None
    reserved_tokens: NonNegativeInt = 0

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        if self.finished_at - self.started_at != timedelta(
            milliseconds=self.elapsed_ms
        ):
            raise ValueError("Время попытки не соответствует elapsed_ms")
        if self.outcome == "success":
            if self.error_code is not None or self.generation_fingerprint is None:
                raise ValueError("Успешная попытка требует generation hash без ошибки")
        elif self.generation_fingerprint is not None:
            raise ValueError("Неуспешная попытка не подтверждает generation")
        if (self.outcome == "failed") != (self.error_code is not None):
            raise ValueError("error_code разрешён и обязателен только для failure")
        if (self.input_tokens is None) != (self.output_tokens is None):
            raise ValueError("Usage требует обеих величин либо двух None")
        identities = (self.provider_id, self.provider_version, self.model_id)
        if any(value is None for value in identities) and any(
            value is not None for value in identities
        ):
            raise ValueError("Provider identity требует id, version и model")
        return self


class LLMRoutingMode(StrEnum):
    """Детерминированный выбор и явно разрешённое переключение deployments."""

    FIXED = "fixed"
    NO_LLM = "no_llm"
    LOCAL_ONLY = "local_only"
    PRIVACY_FIRST = "privacy_first"
    FALLBACK = "fallback"


class LLMBudget(FrozenContract):
    """Общий предел run; retries также расходуют calls, tokens и deadline."""

    max_calls: Annotated[PositiveInt, Field(le=1000)]
    max_tokens: Annotated[PositiveInt, Field(le=100_000_000)]
    max_time_ms: Annotated[PositiveInt, Field(le=300_000)]


class LLMRoutePolicy(FrozenContract):
    """Allowlist конкретного deployment, включая capabilities fingerprint."""

    capabilities_fingerprint: FingerprintStr
    allowed_classifications: tuple[DataClassification, ...]

    @model_validator(mode="after")
    def _unique_classifications(self) -> Self:
        if not self.allowed_classifications or len(
            set(self.allowed_classifications)
        ) != len(self.allowed_classifications):
            raise ValueError(
                "Route требует непустую уникальную classification allowlist"
            )
        return self


class LLMRoutingPolicy(FrozenContract):
    """Fingerprint policy связывает весь порядок destinations и общий бюджет."""

    policy_id: IdentifierStr
    mode: LLMRoutingMode
    routes: Annotated[tuple[LLMRoutePolicy, ...], Field(max_length=16)]
    budget: LLMBudget

    @model_validator(mode="after")
    def _validate_routes(self) -> Self:
        if self.mode is LLMRoutingMode.NO_LLM:
            if self.routes:
                raise ValueError("no_llm запрещает routes")
            return self
        if not self.routes or (
            self.mode is LLMRoutingMode.FIXED and len(self.routes) != 1
        ):
            raise ValueError(
                "fixed требует одну route; остальные режимы — непустой список"
            )
        fingerprints = [route.capabilities_fingerprint for route in self.routes]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("Повторный deployment запрещён")
        return self
