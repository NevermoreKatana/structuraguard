"""Закрытый wire format HMAC audit v1; raw payload и key material не допускаются."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    DataClassification,
    FingerprintStr,
    UtcDateTime,
)

type AuditDigest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
type AuditCount = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]


class AuditKind(StrEnum):
    """Закрытые стадии audit lifecycle; наличие события не доказывает полноту run."""

    RUN_STARTED = "run_started"
    SECURITY_DECISION = "security_decision"
    PARSER_FINISHED = "parser_finished"
    LLM_FINISHED = "llm_finished"
    VALIDATION_FINISHED = "validation_finished"
    LOAD_COMMITTED = "load_committed"
    RUN_FINISHED = "run_finished"
    RUN_FAILED = "run_failed"


class AuditValidationSummary(FrozenContract):
    """Только counts; paths, messages и validation samples исключены."""

    checked: AuditCount
    accepted: AuditCount
    rejected: AuditCount

    @model_validator(mode="after")
    def consistent(self) -> Self:
        """Проверить сумму accepted/rejected; несовпадение даёт ValueError."""
        if self.accepted + self.rejected != self.checked:
            raise ValueError("Audit validation counts не согласованы")
        return self


class SecurityAuditEvent(FrozenContract):
    """Safe evidence от trusted owner; UUID ссылаются на отдельный registry.

    Provider/model/prompt/actor представлены opaque IDs, а не внешними именами.
    Fingerprints относятся к артефактам и policy, не к коротким raw secrets.
    Signed event удостоверяет заявление владельца, но не истинность input.
    """

    version: Literal[1] = 1
    event_id: UUID
    run_id: UUID
    actor_id: UUID
    occurred_at: UtcDateTime
    sdk_version: Literal["0.3.0"] = "0.3.0"
    kind: AuditKind
    status: Literal[
        "started", "completed", "rejected", "review", "failed", "cancelled"
    ] = "started"
    decision: Literal["allowed", "blocked", "review", "error"] | None = None
    policy_fingerprint: FingerprintStr
    source_fingerprint: FingerprintStr | None = None
    database_fingerprint: FingerprintStr | None = None
    mapping_fingerprint: FingerprintStr | None = None
    target_id: UUID | None = None
    provider_id: UUID | None = None
    model_id: UUID | None = None
    prompt_id: UUID | None = None
    classification: DataClassification | None = None
    validation: AuditValidationSummary | None = None
    inserted: AuditCount = 0
    updated: AuditCount = 0

    @model_validator(mode="after")
    def evidence(self) -> Self:
        """Проверить stage/status/evidence до подписи; несовместимость даёт ValueError."""
        if self.kind is AuditKind.RUN_STARTED:
            if (
                self.status != "started"
                or self.decision is not None
                or any(
                    (
                        self.source_fingerprint,
                        self.database_fingerprint,
                        self.mapping_fingerprint,
                        self.validation,
                        self.inserted,
                        self.updated,
                    )
                )
            ):
                raise ValueError(
                    "Начальный audit event не принимает evidence будущих стадий"
                )
        elif self.status == "started" or self.decision is None:
            raise ValueError("Audit outcome требует status и decision")
        if (
            self.decision is not None
            and self.status
            not in {
                "allowed": {"completed"},
                "blocked": {"rejected"},
                "review": {"review"},
                "error": {"failed", "cancelled"},
            }[self.decision]
        ):
            raise ValueError("Audit status и decision не согласованы")
        if self.kind is AuditKind.LOAD_COMMITTED:
            if self.status != "completed" or any(
                value is None
                for value in (
                    self.source_fingerprint,
                    self.database_fingerprint,
                    self.mapping_fingerprint,
                    self.target_id,
                    self.validation,
                )
            ):
                raise ValueError("Load audit требует полное evidence")
        elif self.inserted or self.updated:
            raise ValueError("Write counts принадлежат только committed load")
        if self.kind is AuditKind.LLM_FINISHED and any(
            value is None
            for value in (
                self.provider_id,
                self.model_id,
                self.prompt_id,
                self.classification,
            )
        ):
            raise ValueError("LLM audit требует routing evidence")
        if self.kind is AuditKind.RUN_FAILED and self.status not in {
            "failed",
            "cancelled",
        }:
            raise ValueError("Failed event требует failure outcome")
        return self


class AuditHead(FrozenContract):
    """Trusted anchor хранится независимо от проверяемого store."""

    chain_id: UUID
    sequence: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    digest: AuditDigest


class AuditEnvelope(FrozenContract):
    """Неизменяемый wire envelope HMAC v1 без ключа и произвольного payload.

    UUID, sequence, event, previous_hash и current_hash валидируются Pydantic.
    Создание не подписывает событие и не даёт доверия; используйте AuditChain."""

    version: Literal[1] = 1
    algorithm: Literal["hmac-sha256"] = "hmac-sha256"
    chain_id: UUID
    sequence: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
    key_id: UUID
    event: SecurityAuditEvent
    previous_hash: AuditDigest
    current_hash: AuditDigest

    @property
    def head(self) -> AuditHead:
        """Вернуть chain/sequence/digest; надёжность anchor зависит от его хранения."""
        return AuditHead(
            chain_id=self.chain_id, sequence=self.sequence, digest=self.current_hash
        )


class AuditVerification(FrozenContract):
    """Без anchor подтверждается только целостность представленного prefix."""

    head: AuditHead
    count: AuditCount
    anchored: bool
    coverage: Literal["provided_prefix", "trusted_head"]
