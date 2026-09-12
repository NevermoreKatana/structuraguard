"""Ограниченные immutable проекции записей для независимых уровней M12/C."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract
from .common import FingerprintStr, IdentifierStr, NormalizedScalar, ValidationIssue

type RuleValueType = Literal[
    "string", "integer", "decimal", "number", "boolean", "date", "datetime"
]


class RecordValidationLimits(FrozenContract):
    """Общие конечные budgets; превышение отменяет выдачу частичного отчёта."""

    max_records: Annotated[StrictInt, Field(ge=1, le=10000)] = 1000
    max_rules: Annotated[StrictInt, Field(ge=1, le=512)] = 128
    max_issues: Annotated[StrictInt, Field(ge=1, le=10000)] = 1024
    max_keys: Annotated[StrictInt, Field(ge=1, le=10000)] = 2048
    max_evaluations: Annotated[StrictInt, Field(ge=1, le=1000000)] = 100000
    max_nodes: Annotated[StrictInt, Field(ge=1, le=1000000)] = 100000
    max_bytes: Annotated[StrictInt, Field(ge=1, le=16777216)] = 4194304
    max_text_chars: Annotated[StrictInt, Field(ge=1, le=65536)] = 4096
    max_numeric_digits: Annotated[StrictInt, Field(ge=1, le=512)] = 128
    max_decimal_exponent: Annotated[StrictInt, Field(ge=1, le=2048)] = 512


class ValidationCell(FrozenContract):
    """Отсутствующая cell означает missing; NullScalar означает explicit null."""

    field_id: IdentifierStr
    value: NormalizedScalar = Field(repr=False)


class ValidationRecord(FrozenContract):
    """Проекция caller: collection_id соответствует table_id на уровне DB."""

    record_id: IdentifierStr
    collection_id: IdentifierStr
    parent_id: IdentifierStr | None = None
    values: tuple[ValidationCell, ...] = Field(max_length=512, repr=False)

    @model_validator(mode="after")
    def distinct_fields(self) -> Self:
        if len({c.field_id for c in self.values}) != len(self.values):
            raise ValueError("Повторяющееся поле записи")
        return self


class ValidationDataset(FrozenContract):
    """Завершённый scope (EOF), объединяющий batches; raw/trace остаются у caller."""

    records: tuple[ValidationRecord, ...] = Field(max_length=10000, repr=False)

    @model_validator(mode="after")
    def identities(self) -> Self:
        ids = {r.record_id for r in self.records}
        if len(ids) != len(self.records):
            raise ValueError("Record ID должен быть уникален во всём scope")
        parents = {r.record_id: r.parent_id for r in self.records}
        for record in self.records:
            seen = {record.record_id}
            parent = record.parent_id
            while parent is not None:
                if parent not in ids or parent in seen or len(seen) > 32:
                    raise ValueError(
                        "Parent scope отсутствует, цикличен или слишком глубок"
                    )
                seen.add(parent)
                parent = parents[parent]
        return self


class RecordValidationIssue(FrozenContract):
    """All-errors location без исходных значений, metadata SQL и driver errors."""

    issue: ValidationIssue
    record_id: IdentifierStr | None = None
    collection_id: IdentifierStr | None = None
    field_ids: tuple[IdentifierStr, ...] = Field(default=(), max_length=512)
    rule_id: IdentifierStr | None = None

    @property
    def code(self) -> str:
        """Вернуть code вложенного ValidationIssue без раскрытия record values."""
        return self.issue.code


class RecordValidationResult(FrozenContract):
    """Advisory отчёт; acceptance не даёт прав writer и не устраняет TOCTOU."""

    input_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    issues: tuple[RecordValidationIssue, ...] = Field(
        default=(), max_length=10000, repr=False
    )
    read_snapshot_fingerprint: FingerprintStr | None = None

    @property
    def accepted(self) -> bool:
        """Проверить отсутствие issues в этом scope; не выдаёт разрешение на загрузку."""
        return not self.issues
