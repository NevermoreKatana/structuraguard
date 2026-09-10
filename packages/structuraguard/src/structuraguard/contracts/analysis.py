"""Закрытые операции и воспроизводимая policy deterministic analysis M5-B."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    ConfidenceDecimal,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PhysicalSourceRef,
    PositiveInt,
)
from structuraguard.contracts.structure import StructuralProfilingOptions


class StructureAnalysisOptions(FrozenContract):
    """Настроить порог confidence, profiling budgets и максимальные bytes plan.

    confidence_threshold по умолчанию 0.85 включительно, диапазон [0, 1].
    Снижение порога не разрешает sampling gaps или выбор между кандидатами.
    max_plan_bytes по умолчанию 1 MiB, hard cap 4 MiB. Неверные значения дают
    Pydantic ValidationError; создание options не выполняет I/O.
    """

    confidence_threshold: ConfidenceDecimal = Decimal("0.85")
    profiling: StructuralProfilingOptions = StructuralProfilingOptions()
    max_plan_bytes: Annotated[PositiveInt, Field(le=4_194_304)] = 1_048_576


class TreePathOperation(StrEnum):
    """Единственные разрешённые переходы: literal raw key и элемент array."""

    KEY = "key"
    ITEM = "item"


class TreeStep(FrozenContract):
    """Literal name никогда не интерпретируется как JSONPath/XPath или код."""

    operation: TreePathOperation
    name: Annotated[StrictStr, Field(max_length=4_096)] = ""
    occurrence: NonNegativeInt = 0

    @model_validator(mode="after")
    def _validate_item(self) -> Self:
        if self.operation is TreePathOperation.ITEM and (self.name or self.occurrence):
            raise ValueError("item не принимает name/occurrence")
        return self


class RecordOperation(StrEnum):
    """Группировка только перечисленных физических refs в указанном порядке."""

    GROUP_REFS = "group_refs"


class ExplicitRecordGrouping(FrozenContract):
    """Конечный scope plan; gaps и границы batches не создают новые records."""

    kind: Literal["explicit_records"] = "explicit_records"
    operation: RecordOperation = RecordOperation.GROUP_REFS
    records: Annotated[
        tuple[
            Annotated[
                tuple[PhysicalSourceRef, ...], Field(min_length=1, max_length=64)
            ],
            ...,
        ],
        Field(min_length=1, max_length=1_000),
    ]

    @model_validator(mode="after")
    def _validate_refs(self) -> Self:
        refs = tuple(ref for record in self.records for ref in record)
        if len(refs) > 1_000 or len(refs) != len(set(refs)):
            raise ValueError("explicit records требуют до 1000 уникальных refs")
        if len({ref.extraction_id for ref in refs}) != 1:
            raise ValueError("explicit records смешивают extraction")
        return self


class LogRecordSelector(FrozenContract):
    """Сохраняет raw текст всех строк record без удаления whitespace и tokens."""

    kind: Literal["log_record"] = "log_record"


class AnalysisScore(FrozenContract):
    """Confidence равен min(boundary, regularity, coverage); это score, не вероятность."""

    policy: Literal["structural_min_v1"] = "structural_min_v1"
    options_fingerprint: FingerprintStr
    boundary: ConfidenceDecimal
    regularity: ConfidenceDecimal
    coverage: ConfidenceDecimal
    observation_ids: Annotated[
        tuple[IdentifierStr, ...], Field(min_length=1, max_length=64)
    ]
    blockers: Annotated[tuple[IdentifierStr, ...], Field(max_length=16)] = ()

    @property
    def confidence(self) -> Decimal:
        """Вернуть минимум boundary, regularity и coverage; это не вероятность."""
        return min(self.boundary, self.regularity, self.coverage)


class PlanDerivation(FrozenContract):
    """Связывает plan с policy, исходным profile и неразрешённой семантикой."""

    rule: IdentifierStr
    options_fingerprint: FingerprintStr
    input_profile_fingerprint: FingerprintStr
    candidate_id: IdentifierStr
    score: AnalysisScore
    unresolved_fields: Annotated[
        tuple[IdentifierStr, ...], Field(min_length=1, max_length=1_024)
    ]
