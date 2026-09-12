"""Bounded intake и общий all-errors collector без раскрытия raw values."""

import math
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from structuraguard.contracts import business_rules, common, database, record_validation
from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import IssueSeverity, ValidationIssue
from structuraguard.contracts.record_validation import (
    RecordValidationIssue,
    RecordValidationLimits,
    RecordValidationResult,
    ValidationDataset,
    ValidationRecord,
)
from structuraguard.exceptions import ValidationError


def failure(code: str = "RULE_INPUT_INVALID") -> ValidationError:
    return ValidationError(
        error_code=code,
        message="Проверка записей отклонена на детерминированной границе.",
    )


def checked[T: FrozenContract](
    value: T,
    expected: type[T],
    limits: RecordValidationLimits,
    *,
    code: str = "RULE_INPUT_INVALID",
) -> T:
    """До сериализации отклонить forged DTO, aliases, cycles и огромные scalars."""
    from structuraguard.contracts import constraint_validation

    if type(value) is not expected:
        raise failure(code)
    # Только классы известных модулей SDK; __module__ пользовательского subclass
    # не удостоверяет происхождение и не позволяет вызвать его serializer.
    # Membership по id не вызывает __hash__/__eq__ чужого metaclass.
    approved = frozenset(
        id(item)
        for module in (
            common,
            database,
            record_validation,
            business_rules,
            constraint_validation,
        )
        for item in vars(module).values()
        if isinstance(item, type) and issubclass(item, FrozenContract)
    )
    enum_types = frozenset(
        id(item)
        for item in vars(common).values()
        if isinstance(item, type) and issubclass(item, StrEnum)
    )
    pending: list[tuple[object, int]] = [(value, 0)]
    nodes = chars = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > limits.max_nodes or depth > 32:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        children: tuple[object, ...] = ()
        if id(type(item)) in approved:
            values = object.__getattribute__(item, "__dict__")
            if type(values) is not dict or len(values) > 128:
                raise failure(code)
            children = tuple(values.values())
        elif type(item) is tuple:
            if len(item) > limits.max_nodes:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            children = item
        elif type(item) is str or id(type(item)) in enum_types:
            assert isinstance(item, str)
            if len(item) > limits.max_text_chars:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            chars += len(item)
            if chars > limits.max_bytes:
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is int:
            if (
                item.bit_length() > limits.max_numeric_digits * 4
                or len(str(abs(item))) > limits.max_numeric_digits
            ):
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is Decimal:
            parts = item.as_tuple()
            if not item.is_finite() or not isinstance(parts.exponent, int):
                raise failure(code)
            if (
                len(parts.digits) > limits.max_numeric_digits
                or abs(parts.exponent) > limits.max_decimal_exponent
            ):
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is datetime:
            if item.tzinfo is not UTC:
                raise failure(code)
        elif type(item) is float:
            if not math.isfinite(item):
                raise failure(code)
        elif item is not None and type(item) is not bool and type(item) is not date:
            raise failure(code)
        # Учитываем ещё не посещённые nodes до выделения очереди: shared tuple
        # fanout иначе умножает память на depth при малом числе посещений.
        if len(pending) + len(children) > limits.max_nodes - nodes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        pending.extend((child, depth + 1) for child in children)
    try:
        payload = value.model_dump_json(warnings="error")
        if len(payload.encode("utf-8")) > limits.max_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return expected.model_validate_json(payload)
    except (ValueError, TypeError, AttributeError, OverflowError):
        pass
    raise failure(code)


class Collector:
    def __init__(self, limits: RecordValidationLimits) -> None:
        self.limits = limits
        self.issues: list[RecordValidationIssue] = []
        self.evaluations = 0
        self.keys = 0

    def tick(self, amount: int = 1) -> None:
        self.evaluations += amount
        if self.evaluations > self.limits.max_evaluations:
            raise failure("SECURITY_LIMIT_EXCEEDED")

    def key(self) -> None:
        self.keys += 1
        if self.keys > self.limits.max_keys:
            raise failure("SECURITY_LIMIT_EXCEEDED")

    def add(
        self,
        code: str,
        row: ValidationRecord | None = None,
        fields: tuple[str, ...] = (),
        rule_id: str | None = None,
        *,
        collection_id: str | None = None,
    ) -> None:
        if len(self.issues) >= self.limits.max_issues:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        self.issues.append(
            RecordValidationIssue(
                issue=ValidationIssue(
                    code=code, severity=IssueSeverity.ERROR, message_key=code
                ),
                record_id=row.record_id if row else None,
                collection_id=row.collection_id if row else collection_id,
                field_ids=fields,
                rule_id=rule_id,
            )
        )

    def result(
        self, data: ValidationDataset, policy: str, snapshot: str | None = None
    ) -> RecordValidationResult:
        return RecordValidationResult(
            input_fingerprint=canonical_sha256_value(data.canonical_json()),
            policy_fingerprint=policy,
            issues=tuple(self.issues),
            read_snapshot_fingerprint=snapshot,
        )
