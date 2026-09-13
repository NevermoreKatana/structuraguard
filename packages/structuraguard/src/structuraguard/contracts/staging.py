"""Версионированные metadata staging; артефакты хранит доверенный владелец retention.

DTO проверяются Pydantic без I/O; неверная структура даёт ValidationError.
Скрытый repr не делает полную JSON-сериализацию refs/identifiers безопасной для logs.
Сроки хранения — обязательство владельца artifacts, а не чтение или удаление payload.
"""

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import (
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PositiveInt,
    UtcDateTime,
)
from .database import StagingContext
from .normalized import NormalizedBatchSummary


class _Sensitive(FrozenContract):
    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())


class StagingArtifactKind(StrEnum):
    """Закрытые категории внешних immutable artifacts."""

    RAW = "raw"
    NORMALIZED = "normalized"
    MAPPING = "mapping"
    VALIDATION = "validation"
    PROVENANCE = "provenance"


class StagingRunStatus(StrEnum):
    """Состояние staging attempt, не свидетельство commit целевой БД."""

    OPEN = "open"
    SEALED = "sealed"
    EXECUTING = "executing"
    COMMITTED = "committed"
    QUARANTINED = "quarantined"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"
    EXPIRED = "expired"


class StagingArtifactReference(_Sensitive):
    """Непрозрачная ссылка; store не открывает artifact_id как path/URL.

    retained_until — обязательство владельца artifact. Fingerprint относится
    к артефакту целиком; record/batch ordinals задают selection. JSON чувствителен.
    """

    kind: StagingArtifactKind
    artifact_id: IdentifierStr
    fingerprint: FingerprintStr
    retained_until: UtcDateTime


class StagingRetentionPolicy(FrozenContract):
    """Неизменяемая policy хранения metadata/refs, заданная владельцем store.

    Args:
        retain_kinds: Обязательные категории refs; пустой tuple — metadata-only.
        success_seconds: Хранение после COMMITTED, по умолчанию сутки.
        failure_seconds: Хранение после прочих terminal outcomes, включая quarantine;
            по умолчанию семь суток.
        max_run_seconds: Допустимый срок нового run от begin, по умолчанию сутки.

    Pydantic отклоняет неверные пределы до I/O. Cleanup удаляет refs и index,
    сохраняя run tombstone. Удаление внешнего payload выполняет его владелец.
    EXECUTING/UNKNOWN не очищаются автоматически; policy не меняется per-run.
    """

    retain_kinds: tuple[StagingArtifactKind, ...] = tuple(StagingArtifactKind)
    success_seconds: Annotated[NonNegativeInt, Field(le=31_536_000)] = 86_400
    failure_seconds: Annotated[NonNegativeInt, Field(le=31_536_000)] = 604_800
    max_run_seconds: Annotated[PositiveInt, Field(le=604_800)] = 86_400

    @model_validator(mode="after")
    def canonical_kinds(self) -> Self:
        """Канонизировать категории policy без повторов и внешних эффектов."""
        object.__setattr__(
            self,
            "retain_kinds",
            tuple(k for k in StagingArtifactKind if k in self.retain_kinds),
        )
        return self

    @property
    def fingerprint(self) -> str:
        """Связать run с неизменяемой retention policy."""
        return canonical_sha256_value(self)


class StagingLimits(FrozenContract):
    """Конечные бюджеты metadata и входного NormalizedBatch, без скрытого spill."""

    max_records: Annotated[PositiveInt, Field(le=100_000)] = 10_000
    max_batches: Annotated[PositiveInt, Field(le=10_000)] = 1_000
    max_batch_records: Annotated[PositiveInt, Field(le=10_000)] = 500
    max_page_records: Annotated[PositiveInt, Field(le=10_000)] = 500
    max_input_bytes: Annotated[PositiveInt, Field(le=67_108_864)] = 8_388_608
    max_nodes: Annotated[PositiveInt, Field(le=1_000_000)] = 100_000
    timeout_seconds: Annotated[PositiveInt, Field(le=300)] = 30
    cleanup_seconds: Annotated[PositiveInt, Field(le=30)] = 5


class StagingRunSpec(_Sensitive):
    """Неизменяемая спецификация run с ожидаемыми counts и внешними references.

    context связывает target, policy, normalized snapshot, expiry и лимиты;
    source/extraction/parse/mapping fingerprints связывают остальные artifacts.
    batch_count/record_count относятся ко всему run, включая terminal batch.
    references содержит не более одной ссылки каждой категории.

    Pydantic проверяет структуру/связи DTO без I/O. begin дополнительно сверяет
    retention, сроки и бюджеты. Source payload не читается, load не разрешается.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    context: StagingContext
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    mapping_plan_fingerprint: FingerprintStr
    batch_count: Annotated[PositiveInt, Field(le=10_000)]
    record_count: Annotated[NonNegativeInt, Field(le=100_000)]
    references: Annotated[tuple[StagingArtifactReference, ...], Field(max_length=5)]

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        """Сверить refs/counts с context и упорядочить категории без artifact I/O."""
        if len({r.kind for r in self.references}) != len(self.references):
            raise ValueError("Категория artifact повторяется")
        if self.record_count > self.context.max_records:
            raise ValueError("Run превышает record budget context")
        for ref in self.references:
            if (
                ref.kind is StagingArtifactKind.NORMALIZED
                and ref.fingerprint != self.context.normalized_fingerprint
            ):
                raise ValueError("Normalized reference не соответствует context")
            if (
                ref.kind is StagingArtifactKind.MAPPING
                and ref.fingerprint != self.mapping_plan_fingerprint
            ):
                raise ValueError("Mapping reference не соответствует plan")
        object.__setattr__(
            self, "references", tuple(sorted(self.references, key=lambda r: r.kind))
        )
        return self


class StagingRun(_Sensitive):
    """Run metadata; tombstone сохраняет counts и исходный spec fingerprint."""

    spec: StagingRunSpec
    spec_fingerprint: FingerprintStr
    retention_fingerprint: FingerprintStr
    status: StagingRunStatus = StagingRunStatus.OPEN
    revision: NonNegativeInt = 0
    batch_count: NonNegativeInt = 0
    record_count: NonNegativeInt = 0
    created_at: UtcDateTime
    updated_at: UtcDateTime
    sealed_fingerprint: FingerprintStr | None = None
    cleanup_after: UtcDateTime | None = None
    purged: StrictBool = False


class StagedBatch(_Sensitive):
    """Summary NormalizedBatch без исходных scalar values."""

    summary: NormalizedBatchSummary
    created_at: UtcDateTime


class StagedRecord(_Sensitive):
    """Record metadata со ссылками на run artifacts и selection по ordinals/ID."""

    record_id: IdentifierStr
    record_index: NonNegativeInt
    batch_index: NonNegativeInt
    index_in_batch: NonNegativeInt
    record_fingerprint: FingerprintStr
    status: Literal["staged"] = "staged"
    references: tuple[StagingArtifactReference, ...]
    created_at: UtcDateTime
