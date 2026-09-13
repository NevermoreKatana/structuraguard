"""Неизменяемая policy ресурсов и audit metadata без исходных значений."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import FingerprintStr, UtcDateTime


class Resource(StrEnum):
    """Закрытые единицы учёта; время измеряется в миллисекундах."""

    FILE_BYTES = "file_bytes"
    STREAM_BYTES = "stream_bytes"
    RECORDS = "records"
    COLUMNS = "columns"
    NESTING_DEPTH = "nesting_depth"
    TEXT_CHARS = "text_chars"
    CHUNKS = "chunks"
    PARSER_TIME_MS = "parser_time_ms"
    LLM_CALLS = "llm_calls"
    LLM_TOKENS = "llm_tokens"
    LLM_TIME_MS = "llm_time_ms"
    DB_BATCH_ROWS = "db_batch_rows"
    DB_BATCH_BYTES = "db_batch_bytes"
    DB_QUERIES = "db_queries"
    PROCESSING_TIME_MS = "processing_time_ms"


class SecurityLimits(FrozenContract):
    """Общие максимумы; более строгие adapter limits сохраняют приоритет.

    Bytes — октеты, text_chars — Unicode code points, chunks — physical batches.
    Records имеют существующую семантику выбранного parser; columns ограничивают
    ширину таблицы/объекта. LLM tokens — резерв input + output, не invoice.
    DB batch limits относятся к одному statement, queries — к одному run.
    Ноль, bool, строки и превышение абсолютных caps запрещены.
    """

    max_file_bytes: Annotated[StrictInt, Field(gt=0, le=1_000_000_000)] = 50_000_000
    max_stream_bytes: Annotated[StrictInt, Field(gt=0, le=1_000_000_000)] = 50_000_000
    max_records: Annotated[StrictInt, Field(gt=0, le=1_000_000)] = 1_000_000
    max_columns: Annotated[StrictInt, Field(gt=0, le=10_000)] = 500
    max_nesting_depth: Annotated[StrictInt, Field(gt=0, le=128)] = 30
    max_text_chars: Annotated[StrictInt, Field(gt=0, le=100_000_000)] = 5_000_000
    max_chunks: Annotated[StrictInt, Field(gt=0, le=10_000)] = 10_000
    max_parser_time_ms: Annotated[StrictInt, Field(gt=0, le=300_000)] = 300_000
    max_llm_calls: Annotated[StrictInt, Field(gt=0, le=1000)] = 10
    max_llm_tokens: Annotated[StrictInt, Field(gt=0, le=100_000_000)] = 50_000
    max_llm_time_ms: Annotated[StrictInt, Field(gt=0, le=300_000)] = 300_000
    max_db_batch_rows: Annotated[StrictInt, Field(gt=0, le=1000)] = 100
    max_db_batch_bytes: Annotated[StrictInt, Field(gt=0, le=16_777_216)] = 1_048_576
    max_db_queries: Annotated[StrictInt, Field(gt=0, le=1_000_000)] = 10_000
    max_processing_time_ms: Annotated[StrictInt, Field(gt=0, le=300_000)] = 300_000
    read_chunk_bytes: Annotated[StrictInt, Field(gt=0, le=1_048_576)] = 65_536

    def maximum(self, resource: Resource) -> int:
        """Получить предел только закрытой resource category."""
        if type(resource) is not Resource:
            raise ValueError("Неизвестная resource category")
        value: int = getattr(self, f"max_{resource.value}")
        return value


type ParserFormat = Literal[
    "txt",
    "log",
    "markdown",
    "csv",
    "tsv",
    "json",
    "jsonl",
    "xml",
    "html",
    "yaml",
    "xlsx",
    "pdf",
    "docx",
]


class SecurityPolicy(FrozenContract):
    """Trusted maximum policy; по умолчанию parser execution не разрешён.

    In-process execution требует явных format allowlist и parser_trust=trusted.
    Sandbox implementation этот contract не предоставляет. Policy не выдаёт
    LLM routing или DB permissions и не заменяет их существующие allowlists.
    """

    limits: SecurityLimits = Field(default_factory=SecurityLimits)
    allowed_formats: tuple[ParserFormat, ...] = Field(default=(), max_length=13)
    parser_trust: Literal["sandbox_required", "trusted"] = "sandbox_required"
    strict_mode: bool = Field(default=False, strict=True)
    risky_formats: tuple[ParserFormat, ...] = Field(
        default=("pdf", "docx", "xlsx", "xml", "html", "yaml"), max_length=13
    )

    @model_validator(mode="after")
    def unique_formats(self) -> Self:
        """Отклонить повтор allowed format через ValueError без I/O."""
        if len(set(self.allowed_formats)) != len(self.allowed_formats):
            raise ValueError("Повтор format в allowlist")
        return self

    @property
    def fingerprint(self) -> str:
        """Связать настройки, не включая source/credentials/host handles."""
        return canonical_sha256_value(self)

    def narrow(self, limits: SecurityLimits) -> SecurityPolicy:
        """Создать policy с меньшими caps; расширение даёт ValueError без I/O."""
        checked = SecurityLimits.model_validate(limits.model_dump(warnings="error"))
        if any(
            getattr(checked, name) > getattr(self.limits, name)
            for name in SecurityLimits.model_fields
        ):
            raise ValueError("Per-run limits не могут расширять trusted policy")
        return SecurityPolicy(
            limits=checked,
            allowed_formats=self.allowed_formats,
            parser_trust=self.parser_trust,
            strict_mode=self.strict_mode,
            risky_formats=self.risky_formats,
        )


type ResourceErrorCode = Literal[
    "SECURITY_LIMIT_EXCEEDED",
    "SECURITY_INPUT_REJECTED",
    "SECURITY_POLICY_INVALID",
    "SECURITY_SANDBOX_REQUIRED",
    "PROCESSING_TIMEOUT",
    "SECURITY_RUN_CLOSED",
    "SECURITY_OPERATION_FAILED",
    "SECURITY_OPERATION_CANCELLED",
]


class ResourceAuditEvent(FrozenContract):
    """Одно terminal resource decision; payload, paths и free-form text запрещены.

    Это готовое к передаче audit evidence, а не durable/HMAC chain. Opaque UUID
    назначает trusted composition owner; event не принимает caller text ID.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: Annotated[
        str,
        Field(
            strict=True,
            pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        ),
    ]
    policy_fingerprint: FingerprintStr
    occurred_at: UtcDateTime
    outcome: Literal["rejected", "failed", "cancelled"]
    code: ResourceErrorCode
    resource: Resource | None = None
    limit: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)] | None = None
    observed: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)] | None = None
