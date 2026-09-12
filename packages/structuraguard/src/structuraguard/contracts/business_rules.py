"""Закрытый data-only DSL: операции и operands без executable expressions."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import DecimalScalar, FingerprintStr, IdentifierStr, NormalizedScalar
from .record_validation import RuleValueType


class RuleField(FrozenContract):
    """Декларация типа поля в конкретной коллекции."""

    collection_id: IdentifierStr
    field_id: IdentifierStr
    value_type: RuleValueType


class FieldOperand(FrozenContract):
    """Точное ID поля; точки и скобки не имеют executable семантики."""

    kind: Literal["field"] = "field"
    field_id: IdentifierStr
    value_type: RuleValueType


class LiteralOperand(FrozenContract):
    """Tagged константа; строки никогда не разбираются как Python или SQL."""

    kind: Literal["literal"] = "literal"
    value: NormalizedScalar = Field(repr=False)


type RuleOperand = Annotated[FieldOperand | LiteralOperand, Field(discriminator="kind")]


class ProductOperand(FrozenContract):
    """Ограниченное произведение точных integer/Decimal operands."""

    kind: Literal["product"] = "product"
    factors: tuple[RuleOperand, ...] = Field(min_length=1, max_length=8)


type SumTerm = Annotated[
    FieldOperand | LiteralOperand | ProductOperand, Field(discriminator="kind")
]


class _Rule(FrozenContract):
    rule_id: IdentifierStr
    collection_id: IdentifierStr


class ComparisonRule(_Rule):
    """Сравнение: reject null, явное equal либо pass (например SQL CHECK UNKNOWN)."""

    op: Literal["equals", "not_equals", "gt", "gte", "lt", "lte", "date_lt", "date_lte"]
    left: RuleOperand
    right: RuleOperand
    nulls: Literal["reject", "equal", "pass"] = "reject"


class RequiredIfRule(_Rule):
    """Поле обязательно при равенстве двух typed operands; null condition явен."""

    op: Literal["required_if"] = "required_if"
    field_id: IdentifierStr
    left: RuleOperand
    right: RuleOperand
    nulls: Literal["reject", "equal", "pass"] = "reject"


class PresenceRule(_Rule):
    """Presence означает существующую non-null cell; пустая строка присутствует."""

    op: Literal["mutually_exclusive", "at_least_one"]
    field_ids: tuple[IdentifierStr, ...] = Field(min_length=1, max_length=64)


class SumEqualsRule(_Rule):
    """Сумма children конкретного parent; пустая сумма 0, null не пропускается."""

    op: Literal["sum_equals"] = "sum_equals"
    total: RuleOperand
    item_collection_id: IdentifierStr
    terms: tuple[SumTerm, ...] = Field(min_length=1, max_length=16)
    tolerance: DecimalScalar = Field(
        default_factory=lambda: DecimalScalar(value=Decimal(0))
    )

    @model_validator(mode="after")
    def positive_tolerance(self) -> Self:
        if self.tolerance.value < 0:
            raise ValueError("Tolerance должна быть неотрицательной")
        return self


class ItemCountRule(_Rule):
    """Количество children в явно указанной коллекции данного parent."""

    op: Literal["min_items", "max_items"]
    item_collection_id: IdentifierStr
    count: Annotated[StrictInt, Field(ge=0, le=10000)]


class UniqueByRule(_Rule):
    """Exact ordered composite key; scope и null semantics обязательны."""

    op: Literal["unique_by"] = "unique_by"
    fields: tuple[FieldOperand, ...] = Field(min_length=1, max_length=32)
    scope: Literal["collection", "parent"]
    nulls: Literal["distinct", "equal", "reject"]


class MatchesReferenceRule(_Rule):
    """Ссылка на trusted immutable snapshot, без URL/path и retrieval."""

    op: Literal["matches_reference"] = "matches_reference"
    fields: tuple[FieldOperand, ...] = Field(min_length=1, max_length=32)
    reference_id: IdentifierStr
    reference_fingerprint: FingerprintStr
    nulls: Literal["reject", "equal"] = "reject"


type BusinessRule = Annotated[
    ComparisonRule
    | RequiredIfRule
    | PresenceRule
    | SumEqualsRule
    | ItemCountRule
    | UniqueByRule
    | MatchesReferenceRule,
    Field(discriminator="op"),
]


class ReferenceKey(FrozenContract):
    """Составной ключ локального справочника."""

    values: tuple[NormalizedScalar, ...] = Field(
        min_length=1, max_length=32, repr=False
    )


class RuleReference(FrozenContract):
    """Trusted snapshot; fingerprint пересчитывается, порядок keys сохраняется."""

    reference_id: IdentifierStr
    value_types: tuple[RuleValueType, ...] = Field(min_length=1, max_length=32)
    keys: tuple[ReferenceKey, ...] = Field(max_length=10000, repr=False)

    @model_validator(mode="after")
    def typed_keys(self) -> Self:
        if any(
            len(k.values) != len(self.value_types)
            or any(
                v.kind not in (t, "null")
                for v, t in zip(k.values, self.value_types, strict=True)
            )
            for k in self.keys
        ):
            raise ValueError("Неверная форма или тип ключа справочника")
        return self

    @property
    def fingerprint(self) -> str:
        """Вычислить hash полного справочника без I/O; hash не анонимизирует keys."""
        return canonical_sha256_value(self.canonical_json())


class BusinessRuleSet(FrozenContract):
    """Замкнутый набор правил; field refs сверяются перед вычислением."""

    fields: tuple[RuleField, ...] = Field(max_length=4096)
    rules: tuple[BusinessRule, ...] = Field(max_length=512)

    @model_validator(mode="after")
    def distinct_ids(self) -> Self:
        if len({(f.collection_id, f.field_id) for f in self.fields}) != len(
            self.fields
        ) or len({r.rule_id for r in self.rules}) != len(self.rules):
            raise ValueError("Повторяющиеся field/rule IDs")
        return self
