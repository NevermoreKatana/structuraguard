"""Intake provenance: конечные бюджеты и запрет пользовательских serializers."""

import math
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from structuraguard.contracts import (
    analysis,
    common,
    document_semantics,
    execution,
    llm,
    normalization,
    normalized,
    parsing,
    profiling,
    provenance,
    reports,
    semantic,
    source,
    structure,
)
from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.provenance import ProvenanceLimits
from structuraguard.exceptions import ValidationError


def failure(code: str = "PROVENANCE_INPUT_INVALID") -> ValidationError:
    return ValidationError(
        error_code=code, message="Проверка evidence не завершена на безопасной границе."
    )


class BoundedInput:
    """Instance-local budgets охватывают весь snapshot, а не каждый batch отдельно."""

    def __init__(self, limits: ProvenanceLimits) -> None:
        self.limits = limits
        self.nodes = self.size = 0
        modules = (
            analysis,
            common,
            document_semantics,
            execution,
            llm,
            normalization,
            normalized,
            parsing,
            profiling,
            provenance,
            reports,
            semantic,
            source,
            structure,
        )
        # Membership классов по id исключает hooks недоверенного metaclass.
        self.contracts = frozenset(
            id(item)
            for module in modules
            for item in vars(module).values()
            if isinstance(item, type) and issubclass(item, FrozenContract)
        )
        self.enums = frozenset(
            id(item)
            for module in modules
            for item in vars(module).values()
            if isinstance(item, type) and issubclass(item, StrEnum)
        )

    def scan(self, value: object) -> None:
        pending: list[tuple[object, int]] = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            self.nodes += 1
            self.size += 64
            if self.nodes > self.limits.max_nodes or depth > self.limits.max_depth:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            children: tuple[object, ...] = ()
            if id(type(item)) in self.contracts:
                assert isinstance(item, FrozenContract)
                values = object.__getattribute__(item, "__dict__")
                if (
                    type(values) is not dict
                    or len(values) > 128
                    or any(type(name) is not str for name in values)
                    or values.keys() != type(item).model_fields.keys()
                    or object.__getattribute__(item, "__pydantic_extra__") is not None
                ):
                    raise failure()
                children = tuple(values.values())
            elif type(item) is tuple:
                children = item
            elif (
                type(item) is str or id(type(item)) in self.enums or type(item) is bytes
            ):
                assert isinstance(item, str | bytes)
                if len(item) > self.limits.max_scalar_bytes:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                size = len(item.encode("utf-8")) if isinstance(item, str) else len(item)
                if size > self.limits.max_scalar_bytes:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                self.size += size * 4
            elif type(item) is int:
                if (
                    item.bit_length() > self.limits.max_numeric_digits * 4
                    or len(str(abs(item))) > self.limits.max_numeric_digits
                ):
                    raise failure("SECURITY_LIMIT_EXCEEDED")
            elif type(item) is Decimal:
                if not item.is_finite():
                    raise failure()
                parts = item.as_tuple()
                if (
                    not isinstance(parts.exponent, int)
                    or len(parts.digits) > self.limits.max_numeric_digits
                    or abs(parts.exponent) > self.limits.max_decimal_exponent
                ):
                    raise failure("SECURITY_LIMIT_EXCEEDED")
            elif type(item) is datetime:
                if item.tzinfo is not UTC:
                    raise failure()
            elif type(item) is float:
                if not math.isfinite(item):
                    raise failure()
            elif item is not None and type(item) is not bool and type(item) is not date:
                raise failure()
            if self.size + (len(pending) + len(children)) * 64 > self.limits.max_bytes:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            pending.extend((child, depth + 1) for child in children)

    def checked[T: FrozenContract](self, value: T, expected: type[T]) -> T:
        if type(value) is not expected:
            raise failure()
        try:
            self.scan(value)
            payload = value.model_dump(mode="python", warnings="error")
            return expected.model_validate(payload)
        except (ValueError, TypeError, AttributeError, OverflowError):
            pass
        raise failure()
