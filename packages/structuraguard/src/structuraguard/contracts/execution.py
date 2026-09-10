"""Конечные бюджеты execution и точное происхождение выбранных значений."""

from enum import StrEnum
from typing import Annotated

from pydantic import Field

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PhysicalSourceRef,
    PositiveInt,
    RawScalar,
)
from structuraguard.contracts.source import SourceLocation
from structuraguard.contracts.structure import StructuralProfilingOptions


class ParsePlanOptions(FrozenContract):
    """Задать одинаковую ограничивающую policy validator и executor.

    source_limits ограничивает physical replay; остальные поля — plan bytes,
    items/bytes одного record с children, bytes batch, records и число batches.
    Record budget проверяется при накоплении. Sampling budget не разрешает
    пропуск runtime verification. Неверные значения дают Pydantic ValidationError.
    Создание options не выполняет I/O, callbacks или conversions.
    """

    source_limits: StructuralProfilingOptions = StructuralProfilingOptions()
    max_plan_bytes: Annotated[PositiveInt, Field(le=4_194_304)] = 1_048_576
    max_record_bytes: Annotated[PositiveInt, Field(le=8_388_608)] = 1_048_576
    max_record_items: Annotated[PositiveInt, Field(le=100_000)] = 10_000
    max_output_batch_bytes: Annotated[PositiveInt, Field(le=16_777_216)] = 4_194_304
    max_records: Annotated[PositiveInt, Field(le=10_000_000)] = 1_000_000
    max_output_batches: Annotated[PositiveInt, Field(le=100_000)] = 10_000


class SelectionOperation(StrEnum):
    """Закрытый набор операций provenance; не язык исполняемых инструкций."""

    COPY = "copy"
    JOIN_LINES = "join_lines"
    SELECT_TOKEN = "select_token"
    SELECT_KEY = "select_key"
    SELECT_VALUE = "select_value"
    NODE_NAME = "node_name"


class PhysicalValueOrigin(FrozenContract):
    """Raw parent value и реальная location, включая page/cell/path provenance."""

    source_ref: PhysicalSourceRef
    raw_value: RawScalar
    location: SourceLocation


class SelectionTrace(FrozenContract):
    """Закрытая операция и hash selector из bound plan; не исполняемый код."""

    operation: SelectionOperation
    selector_fingerprint: FingerprintStr


class ExecutionStage(StrEnum):
    """Этап отказа executor для машинной обработки без raw exception message."""

    VALIDATION = "validation"
    SOURCE = "source"
    SELECTION = "selection"
    LIMIT = "limit"
    TIMEOUT = "timeout"
    CLEANUP = "cleanup"


class ParseExecutionIssue(FrozenContract):
    """Fatal issue: без raw values, успешного terminal batch и silent skip."""

    code: IdentifierStr
    reason: IdentifierStr
    stage: ExecutionStage
    batch_index: NonNegativeInt | None = None
    emitted_batches: NonNegativeInt = 0
