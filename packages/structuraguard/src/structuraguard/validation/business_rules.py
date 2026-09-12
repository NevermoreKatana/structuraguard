"""Детерминированный DSL evaluator: прямой dispatch, точные числа, никакого I/O."""

from fractions import Fraction

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.business_rules import (
    BusinessRule,
    BusinessRuleSet,
    ComparisonRule,
    FieldOperand,
    ItemCountRule,
    LiteralOperand,
    MatchesReferenceRule,
    PresenceRule,
    ProductOperand,
    RequiredIfRule,
    RuleOperand,
    RuleReference,
    SumEqualsRule,
    UniqueByRule,
)
from structuraguard.contracts.common import (
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    NumberScalar,
)
from structuraguard.contracts.record_validation import (
    RecordValidationLimits,
    RecordValidationResult,
    ValidationDataset,
    ValidationRecord,
)

from ._rule_input import Collector, checked, failure


class BusinessRuleValidator:
    """Выполнить allowlisted rules на полном immutable scope без I/O.

    Args:
        limits: RecordValidationLimits; только None выбирает defaults.
        references: Доверенные справочники для reference_exists, без retrieval.
        max_tolerance: Разрешённая владельцем неотрицательная Decimal tolerance;
            None требует точного равенства сумм.

    Raises:
        ValidationError: Невалидный DTO, references/tolerance или превышение budget.

    References и разрешение ненулевой tolerance задаёт доверенный владелец.
    Caller объединяет batches до EOF и передаёт исходные raw/trace отдельно.
    Budget/malformed DTO дают ValidationError, содержательные ошибки — все issues.
    """

    def __init__(
        self,
        *,
        limits: RecordValidationLimits | None = None,
        references: tuple[RuleReference, ...] = (),
        max_tolerance: DecimalScalar | None = None,
    ) -> None:
        self._limits = checked(
            limits if limits is not None else RecordValidationLimits(),
            RecordValidationLimits,
            RecordValidationLimits(),
        )
        if type(references) is not tuple or len(references) > self._limits.max_rules:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        self._references = tuple(
            checked(r, RuleReference, self._limits) for r in references
        )
        if (
            len({r.reference_id for r in self._references}) != len(self._references)
            or sum(len(r.keys) for r in self._references) > self._limits.max_keys
        ):
            raise failure("RULE_REFERENCE_INVALID")
        self._tolerance = (
            Fraction(checked(max_tolerance, DecimalScalar, self._limits).value)
            if max_tolerance is not None
            else Fraction(0)
        )
        if self._tolerance < 0:
            raise failure("RULE_INPUT_INVALID")

    async def validate(
        self, data: ValidationDataset, *, rules: BusinessRuleSet
    ) -> RecordValidationResult:
        """Собрать все нарушения, missing/null/type issues без изменения значений.

        Args:
            data: Завершённый ValidationDataset после EOF всех его records.
            rules: Typed декларации из закрытого набора операций и field types.

        Returns:
            Immutable RecordValidationResult с привязкой к data и policy.
            accepted подтверждает только переданный набор rules.

        Raises:
            ValidationError: RULE_INPUT_INVALID при malformed DTO либо
                SECURITY_LIMIT_EXCEEDED при исчерпании общего budget.

        I/O, coercion, SQL и выполнение пользовательского кода отсутствуют.
        Missing field отличается от null; его обработка задана типом операции.
        """
        data = checked(data, ValidationDataset, self._limits)
        rules = checked(rules, BusinessRuleSet, self._limits)
        if (
            len(data.records) > self._limits.max_records
            or len(rules.rules) > self._limits.max_rules
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")
        evaluator = _Evaluator(data, rules, self._limits, self._references)
        for rule in rules.rules:
            if (
                isinstance(rule, SumEqualsRule)
                and Fraction(rule.tolerance.value) > self._tolerance
            ):
                evaluator.out.add(
                    "RULE_TOLERANCE_NOT_ALLOWED",
                    rule_id=rule.rule_id,
                    collection_id=rule.collection_id,
                )
                continue
            if evaluator.schema(rule):
                evaluator.run(rule)
        policy = canonical_sha256_value(
            (
                rules.canonical_json(),
                self._limits.canonical_json(),
                tuple(r.fingerprint for r in self._references),
                str(self._tolerance),
            )
        )
        return evaluator.out.result(data, policy)


def scalar_key(value: NormalizedScalar) -> tuple[str, object]:
    """Точное равенство чисел; bool, даты, строки не смешиваются с integer."""
    if isinstance(value, IntegerScalar | DecimalScalar):
        return ("exact_number", Fraction(value.value))
    return (value.kind, value.value)


class _Evaluator:
    def __init__(
        self,
        data: ValidationDataset,
        rules: BusinessRuleSet,
        limits: RecordValidationLimits,
        references: tuple[RuleReference, ...],
    ) -> None:
        self.out = Collector(limits)
        self.fields = {
            (f.collection_id, f.field_id): f.value_type for f in rules.fields
        }
        self.collections: dict[str, list[ValidationRecord]] = {}
        self.children: dict[tuple[str, str], list[ValidationRecord]] = {}
        self.cells = {
            r.record_id: {c.field_id: c.value for c in r.values} for r in data.records
        }
        for row in data.records:
            self.collections.setdefault(row.collection_id, []).append(row)
            if row.parent_id is not None:
                self.children.setdefault((row.parent_id, row.collection_id), []).append(
                    row
                )
        self.references = {r.reference_id: r for r in references}

    def schema(self, rule: BusinessRule) -> bool:
        operands: list[tuple[str, RuleOperand]] = []
        names: tuple[str, ...] = ()
        if isinstance(rule, ComparisonRule | RequiredIfRule):
            operands.extend(
                ((rule.collection_id, rule.left), (rule.collection_id, rule.right))
            )
            if isinstance(rule, RequiredIfRule):
                names = (rule.field_id,)
        elif isinstance(rule, PresenceRule):
            names = rule.field_ids
        elif isinstance(rule, UniqueByRule | MatchesReferenceRule):
            operands.extend((rule.collection_id, f) for f in rule.fields)
        elif isinstance(rule, SumEqualsRule):
            operands.append((rule.collection_id, rule.total))
            for term in rule.terms:
                operands.extend(
                    (rule.item_collection_id, f)
                    for f in (
                        term.factors if isinstance(term, ProductOperand) else (term,)
                    )
                )
        valid = True
        if isinstance(rule, SumEqualsRule | ItemCountRule):
            parents = {
                r.record_id for r in self.collections.get(rule.collection_id, ())
            }
            for child in self.collections.get(rule.item_collection_id, ()):
                self.out.tick()
                if child.parent_id not in parents:
                    self.out.add(
                        "RULE_PARENT_SCOPE_INVALID", child, rule_id=rule.rule_id
                    )
        for collection, operand in operands:
            self.out.tick()
            if (
                isinstance(operand, FieldOperand)
                and self.fields.get((collection, operand.field_id))
                != operand.value_type
            ):
                self.out.add(
                    "RULE_FIELD_UNKNOWN",
                    fields=(operand.field_id,),
                    rule_id=rule.rule_id,
                    collection_id=collection,
                )
                valid = False
        for name in names:
            if (rule.collection_id, name) not in self.fields:
                self.out.add(
                    "RULE_FIELD_UNKNOWN",
                    fields=(name,),
                    rule_id=rule.rule_id,
                    collection_id=rule.collection_id,
                )
                valid = False
        if isinstance(rule, MatchesReferenceRule):
            reference = self.references.get(rule.reference_id)
            if (
                reference is None
                or reference.fingerprint != rule.reference_fingerprint
                or reference.value_types != tuple(f.value_type for f in rule.fields)
            ):
                self.out.add(
                    "RULE_REFERENCE_INVALID",
                    rule_id=rule.rule_id,
                    collection_id=rule.collection_id,
                )
                valid = False
        return valid

    def operand(
        self, operand: RuleOperand, row: ValidationRecord, rule: BusinessRule
    ) -> NormalizedScalar | None:
        self.out.tick()
        if isinstance(operand, LiteralOperand):
            return operand.value
        value = self.cells[row.record_id].get(operand.field_id)
        code = None
        if value is None:
            code = "RULE_MISSING_OPERAND"
        elif value.kind not in (operand.value_type, "null"):
            code = "RULE_TYPE_MISMATCH"
        if code:
            self.out.add(code, row, (operand.field_id,), rule.rule_id)
            return None
        return value

    def compare(
        self, rule: ComparisonRule | RequiredIfRule, row: ValidationRecord
    ) -> bool | None:
        left = self.operand(rule.left, row, rule)
        right = self.operand(rule.right, row, rule)
        if left is None or right is None:
            return None
        op = "equals" if isinstance(rule, RequiredIfRule) else rule.op
        if isinstance(left, NullScalar) or isinstance(right, NullScalar):
            if rule.nulls == "pass":
                return None
            if rule.nulls == "equal" and op in ("equals", "not_equals"):
                equal = isinstance(left, NullScalar) and isinstance(right, NullScalar)
                return equal if op == "equals" else not equal
            self.out.add("RULE_NULL_OPERAND", row, rule_id=rule.rule_id)
            return None
        lkey, rkey = scalar_key(left), scalar_key(right)
        if lkey[0] != rkey[0]:
            self.out.add("RULE_TYPE_MISMATCH", row, rule_id=rule.rule_id)
            return None
        if op in ("equals", "not_equals"):
            return lkey == rkey if op == "equals" else lkey != rkey
        order: int
        if (
            isinstance(left, IntegerScalar | DecimalScalar)
            and isinstance(right, IntegerScalar | DecimalScalar)
            and op not in ("date_lt", "date_lte")
        ):
            order = _order(Fraction(left.value), Fraction(right.value))
        elif (
            isinstance(left, NumberScalar)
            and isinstance(right, NumberScalar)
            and op not in ("date_lt", "date_lte")
        ):
            order = _order(left.value, right.value)
        elif isinstance(left, DateScalar) and isinstance(right, DateScalar):
            order = _order(left.value.toordinal(), right.value.toordinal())
        elif isinstance(left, DateTimeScalar) and isinstance(right, DateTimeScalar):
            order = (left.value > right.value) - (left.value < right.value)
        else:
            self.out.add("RULE_TYPE_MISMATCH", row, rule_id=rule.rule_id)
            return None
        if op in ("lt", "date_lt"):
            return order < 0
        if op in ("lte", "date_lte"):
            return order <= 0
        if op == "gt":
            return order > 0
        return order >= 0

    def number(
        self, operand: RuleOperand, row: ValidationRecord, rule: BusinessRule
    ) -> Fraction | None:
        value = self.operand(operand, row, rule)
        if value is None:
            return None
        if isinstance(value, IntegerScalar | DecimalScalar):
            return Fraction(value.value)
        self.out.add(
            "RULE_NULL_OPERAND"
            if isinstance(value, NullScalar)
            else "RULE_TYPE_MISMATCH",
            row,
            rule_id=rule.rule_id,
        )
        return None

    def sum(self, rule: SumEqualsRule, row: ValidationRecord) -> bool | None:
        total = self.number(rule.total, row, rule)
        accumulated = Fraction(0)
        valid = total is not None
        for child in self.children.get((row.record_id, rule.item_collection_id), ()):
            for term in rule.terms:
                product = Fraction(1)
                for factor in (
                    term.factors if isinstance(term, ProductOperand) else (term,)
                ):
                    number = self.number(factor, child, rule)
                    if number is None:
                        valid = False
                    else:
                        product *= number
                accumulated += product
        if not valid or total is None:
            return None
        return abs(accumulated - total) <= Fraction(rule.tolerance.value)

    def key(
        self, rule: UniqueByRule | MatchesReferenceRule, row: ValidationRecord
    ) -> tuple[tuple[str, object], ...] | None:
        values = [self.operand(f, row, rule) for f in rule.fields]
        if any(v is None for v in values):
            return None
        if any(isinstance(v, NullScalar) for v in values):
            if rule.nulls == "reject":
                self.out.add("RULE_NULL_OPERAND", row, rule_id=rule.rule_id)
                return None
            if rule.nulls == "distinct":
                return None
        return tuple(scalar_key(v) for v in values if v is not None)

    def run(self, rule: BusinessRule) -> None:
        seen: dict[tuple[object, ...], ValidationRecord] = {}
        reported: set[str] = set()
        reference_keys: set[tuple[tuple[str, object], ...]] = set()
        if isinstance(rule, MatchesReferenceRule):
            for reference_key in self.references[rule.reference_id].keys:
                self.out.key()
                reference_keys.add(tuple(scalar_key(v) for v in reference_key.values))
        for row in self.collections.get(rule.collection_id, ()):
            self.out.tick()
            passed: bool | None = True
            if isinstance(rule, ComparisonRule):
                passed = self.compare(rule, row)
            elif isinstance(rule, RequiredIfRule):
                if self.compare(rule, row) is True:
                    passed = self.present(row, rule.field_id)
            elif isinstance(rule, PresenceRule):
                self.out.tick(len(rule.field_ids))
                count = sum(self.present(row, f) for f in rule.field_ids)
                passed = count <= 1 if rule.op == "mutually_exclusive" else count >= 1
            elif isinstance(rule, SumEqualsRule):
                passed = self.sum(rule, row)
            elif isinstance(rule, ItemCountRule):
                count = len(
                    self.children.get((row.record_id, rule.item_collection_id), ())
                )
                passed = (
                    count >= rule.count
                    if rule.op == "min_items"
                    else count <= rule.count
                )
            elif isinstance(rule, UniqueByRule):
                if rule.scope == "parent" and row.parent_id is None:
                    self.out.add("RULE_PARENT_SCOPE_INVALID", row, rule_id=rule.rule_id)
                    continue
                key = self.key(rule, row)
                if key is not None:
                    scope = row.parent_id if rule.scope == "parent" else None
                    full = (scope, *key)
                    self.out.key()
                    previous = seen.get(full)
                    if previous is not None:
                        passed = False
                        if previous.record_id not in reported:
                            self.out.add(
                                "RULE_VIOLATION", previous, rule_id=rule.rule_id
                            )
                            reported.add(previous.record_id)
                    else:
                        seen[full] = row
            elif isinstance(rule, MatchesReferenceRule):
                key = self.key(rule, row)
                passed = key in reference_keys if key is not None else None
            if passed is False:
                self.out.add("RULE_VIOLATION", row, rule_id=rule.rule_id)

    def present(self, row: ValidationRecord, name: str) -> bool:
        value = self.cells[row.record_id].get(name)
        return value is not None and not isinstance(value, NullScalar)


def _order(left: Fraction | float, right: Fraction | float) -> int:
    return (left > right) - (left < right)
