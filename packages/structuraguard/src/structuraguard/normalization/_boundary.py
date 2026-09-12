"""Bounded intake перед сериализацией и вызовом trusted pure extensions."""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum

from structuraguard.contracts import (
    common,
    document_semantics,
    execution,
    normalization,
    normalized,
    source,
)
from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    BooleanScalar,
    BytesScalar,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NullScalar,
    NumberScalar,
    StringScalar,
)
from structuraguard.contracts.normalization import NormalizationLimits
from structuraguard.exceptions import ValidationError


def failure(code: str) -> ValidationError:
    return ValidationError(
        error_code=code, message="Нормализация отклонена на проверяемой границе."
    )


def checked[T: FrozenContract](
    value: T,
    expected: type[T],
    *,
    limits: NormalizationLimits | None = None,
    code: str = "NORMALIZATION_INPUT_INVALID",
) -> T:
    """Отклонить forged/oversized DTO до model_dump и отделить mutable aliases."""
    if type(value) is not expected:
        raise failure(code)
    modules = (common, document_semantics, execution, normalization, normalized, source)
    # Имя __module__ и isinstance не удостоверяют происхождение Python objects.
    # Проверяем identity до обращения к serializers или пользовательским hooks.
    # Храним id, чтобы membership не вызывал hash/eq недоверенного metaclass.
    contracts = frozenset(
        id(item)
        for module in modules
        for item in vars(module).values()
        if isinstance(item, type) and issubclass(item, FrozenContract)
    )
    enums = frozenset(
        id(item)
        for module in modules
        for item in vars(module).values()
        if isinstance(item, type) and issubclass(item, StrEnum)
    )
    bounds = limits or NormalizationLimits(
        max_text_chars=65_536,
        max_numeric_digits=1_024,
        max_decimal_exponent=10_000,
        max_trace_bytes=4_194_304,
    )
    pending: list[tuple[object, int, bool]] = [(value, 0, False)]
    remaining = 20_000
    text_chars = 0
    while pending:
        item, depth, scalar = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > 24:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if id(type(item)) in contracts:
            values = object.__getattribute__(item, "__dict__")
            if type(values) is not dict:
                raise failure(code)
            if len(values) > 128:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            # Даже ключ native dict может выполнить чужой __eq__ ниже.
            if any(type(name) is not str for name in values):
                raise failure(code)
            is_scalar = type(item) in (
                StringScalar,
                IntegerScalar,
                NumberScalar,
                DecimalScalar,
                BooleanScalar,
                DateScalar,
                DateTimeScalar,
                NullScalar,
                BytesScalar,
            )
            pending.extend(
                (child, depth + 1, is_scalar and name == "value")
                for name, child in values.items()
            )
        elif type(item) is tuple:
            if len(item) > 1_024:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            pending.extend((child, depth + 1, False) for child in item)
        elif type(item) is str or type(item) is bytes or id(type(item)) in enums:
            assert isinstance(item, str | bytes)
            if len(item) > (bounds.max_text_chars if scalar else 65_536):
                raise failure("SECURITY_LIMIT_EXCEEDED")
            text_chars += len(item)
            if text_chars > bounds.max_trace_bytes:
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is int:
            digits = bounds.max_numeric_digits if scalar else 1_024
            if item.bit_length() > digits * 4 or len(str(abs(item))) > digits:
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is Decimal:
            if not item.is_finite():
                raise failure(code)
            parts = item.as_tuple()
            if not isinstance(parts.exponent, int):
                raise failure(code)
            if (
                len(parts.digits) > bounds.max_numeric_digits
                or abs(parts.exponent) > bounds.max_decimal_exponent
            ):
                raise failure("SECURITY_LIMIT_EXCEEDED")
        elif type(item) is datetime:
            if item.tzinfo is not UTC:
                raise failure(code)
        elif (
            item is None
            or type(item) is bool
            or type(item) is float
            or type(item) is date
        ):
            pass
        else:
            raise failure(code)
    try:
        payload = value.model_dump_json(warnings="error")
        if len(payload.encode("utf-8")) > bounds.max_trace_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return expected.model_validate_json(payload)
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise failure(code) from None
