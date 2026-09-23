"""Декларативное табличное преобразование перед штатным ingest SDK."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import FingerprintStr, NonNegativeInt, PositiveInt
from structuraguard.contracts.database import CatalogColumnRef
from structuraguard.contracts.deterministic_mapping import (
    Score,
    SensitiveMappingContract,
)

_AUTO = "sha256:" + "0" * 64
type TabularCell = Annotated[StrictStr, Field(max_length=65536)] | None
type TabularReason = Literal[
    "exact_name",
    "normalized_name",
    "typo",
    "semantic_name",
    "split_name",
    "semantic_equivalence",
    "value_context",
    "translated_meaning",
    "composite_component",
    "ambiguous",
    "unsupported",
]
type TabularSourceId = Annotated[StrictStr, Field(pattern=r"^s[0-9]{1,3}$")]
type TabularTargetId = Annotated[StrictStr, Field(pattern=r"^c[0-9]{1,3}$")]
type TabularWireScore = Annotated[
    Decimal, Field(ge=0, le=1, allow_inf_nan=False, max_digits=7, decimal_places=6)
]


class TabularImportOptions(SensitiveMappingContract):
    """Бюджеты запроса; строки целиком проверяет отдельный pure executor."""

    max_sample_rows: Annotated[PositiveInt, Field(le=32)] = 8
    max_sample_chars: Annotated[PositiveInt, Field(le=2048)] = 256
    max_payload_bytes: Annotated[PositiveInt, Field(le=131072)] = 65536
    max_response_bytes: Annotated[PositiveInt, Field(le=131072)] = 32768
    max_seconds: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] = 300
    max_columns: Annotated[PositiveInt, Field(le=128)] = 64


class TabularImportSource(SensitiveMappingContract):
    """Локальный прямоугольный snapshot. repr не раскрывает имена и значения."""

    labels: Annotated[
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=128),
    ]
    rows: Annotated[
        tuple[dict[StrictStr, TabularCell], ...], Field(min_length=1, max_length=5000)
    ]
    fingerprint: FingerprintStr = _AUTO

    @model_validator(mode="after")
    def _snapshot(self) -> Self:
        if len(set(self.labels)) != len(self.labels) or any(
            not s.strip() for s in self.labels
        ):
            raise ValueError("Названия колонок должны быть непустыми и уникальными")
        expected = set(self.labels)
        if any(set(row) != expected for row in self.rows):
            raise ValueError("Каждая строка должна содержать все объявленные колонки")
        size = (
            sum(len(label.encode("utf-8")) for label in self.labels)
            + 8 * len(self.labels)
        ) * (len(self.rows) + 1)
        for row in self.rows:
            size += sum(
                len(v.encode("utf-8")) if v is not None else 4 for v in row.values()
            )
            if size > 8_388_608:
                raise ValueError("Табличный snapshot превышает лимит 8 MiB")
        fingerprint = canonical_sha256_value({"labels": self.labels, "rows": self.rows})
        if self.fingerprint == _AUTO:
            object.__setattr__(self, "fingerprint", fingerprint)
        elif self.fingerprint != fingerprint:
            raise ValueError("Fingerprint табличного snapshot не совпадает")
        return self


class _TabularOperation(SensitiveMappingContract):
    source_id: TabularSourceId
    operation: Literal["copy", "split"]
    split_mode: Literal["whitespace", "literal"] | None
    delimiter: Annotated[StrictStr, Field(min_length=1, max_length=8)] | None
    part_index: Annotated[NonNegativeInt, Field(le=7)] | None
    part_count: Annotated[PositiveInt, Field(le=8)] | None
    confidence: Score
    reason: TabularReason


class TabularImportChoice(_TabularOperation):
    """Wire-выбор LLM только по aliases из переданного bounded payload."""

    target_id: TabularTargetId
    confidence: TabularWireScore


class TabularImportSuggestion(SensitiveMappingContract):
    """Простой JSON-ответ. Межполевые правила проверяет binder, не модель."""

    decision: Literal["map", "ambiguous", "unsupported"]
    assignments: Annotated[tuple[TabularImportChoice, ...], Field(max_length=128)]
    confidence: TabularWireScore
    reason: TabularReason


class TabularImportAssignment(_TabularOperation):
    """Привязанный выбор без SQL и без произвольных выражений."""

    target: CatalogColumnRef


class TabularImportPlan(SensitiveMappingContract):
    """Snapshot-bound план; сам по себе не разрешает запись в БД."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    input_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: StrictStr
    target_policy_fingerprint: FingerprintStr
    scope_fingerprint: FingerprintStr
    assignments: Annotated[
        tuple[TabularImportAssignment, ...], Field(min_length=1, max_length=128)
    ]
    confidence: Score
    fingerprint: FingerprintStr = _AUTO

    @model_validator(mode="after")
    def _binding(self) -> Self:
        fingerprint = canonical_sha256_value(
            self, exclude_top_level=frozenset({"fingerprint"})
        )
        if self.fingerprint == _AUTO:
            object.__setattr__(self, "fingerprint", fingerprint)
        elif self.fingerprint != fingerprint:
            raise ValueError("Fingerprint табличного плана не совпадает")
        return self


class TabularImportLineage(SensitiveMappingContract):
    """Одинаковое правило происхождения для каждой строки snapshot."""

    source_id: TabularSourceId
    source_name: StrictStr
    target: CatalogColumnRef
    target_name: StrictStr
    operation: Literal["copy", "split"]
    part_index: NonNegativeInt | None
    part_count: PositiveInt | None
    confidence: Score
    reason: TabularReason


class TabularImportPreview(SensitiveMappingContract):
    """Локальный before/after содержит исходные значения; не для logs/LLM."""

    row_index: PositiveInt
    before: dict[StrictStr, TabularCell]
    after: dict[StrictStr, TabularCell]


class TabularImportResult(SensitiveMappingContract):
    input_fingerprint: FingerprintStr
    plan_fingerprint: FingerprintStr
    table_id: StrictStr
    rows: tuple[dict[StrictStr, TabularCell], ...]
    lineage: tuple[TabularImportLineage, ...]
    preview: Annotated[tuple[TabularImportPreview, ...], Field(max_length=3)]
    confidence: Score = Decimal(1)
