"""Общие настройки неизменяемых сериализуемых контрактов."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Self, cast

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.config import ExtraValues
from pydantic_core import InitErrorDetails

type CanonicalScalar = str | int | float | bool | None
type CanonicalValue = (
    CanonicalScalar
    | Decimal
    | date
    | datetime
    | bytes
    | Mapping[str, CanonicalValue]
    | Sequence[CanonicalValue]
)
type CanonicalInput = BaseModel | CanonicalValue

_REDACTED_VALIDATION_INPUT = "[REDACTED]"
_REDACTED_VALIDATION_LOCATION = "<redacted>"
_MAX_VALIDATION_CONTEXT_CHARS = 512
_SENSITIVE_VALIDATION_TEXT = re.compile(
    r"(?i)(?:"
    r"\b(?:authorization|dsn|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"aws[_-]?secret[_-]?access[_-]?key|secret|token)\s*[:=]\s*\S+"
    r"|\b(?:bearer|basic)\s+\S+"
    r"|\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{6,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|"
    r"whsec_[A-Za-z0-9]{12,})\b"
    r")"
)
_UNTRUSTED_VALIDATION_CONTEXT_KEYS = frozenset(
    {"actual", "given", "input", "tag", "value"}
)
_SENSITIVE_LOCATION_PARTS = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "dsn",
    "privatekey",
)
_EMAIL_LOCATION = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"(?![A-Z0-9.-])"
)
_PAYMENT_CARD_LOCATION = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Каноническое значение Decimal должно быть конечным")
    if not value:
        return "0"
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("Каноническое значение Decimal должно быть конечным")
    canonical_digits = list(digits)
    while canonical_digits[-1] == 0:
        canonical_digits.pop()
        exponent += 1
    return str(Decimal((sign, tuple(canonical_digits), exponent))).replace("e", "E")


def _sanitize_validation_text(value: str) -> str:
    sanitized = _SENSITIVE_VALIDATION_TEXT.sub(
        _REDACTED_VALIDATION_INPUT,
        value,
    )
    return sanitized[:_MAX_VALIDATION_CONTEXT_CHARS]


def _sanitize_validation_context_value(value: object) -> object:
    if isinstance(value, BaseException):
        return ValueError(_sanitize_validation_text(str(value)))
    if isinstance(value, str):
        return _sanitize_validation_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_validation_context_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, bytes):
        return tuple(_sanitize_validation_context_value(item) for item in value)
    return value


def _sanitize_validation_context(context: Mapping[str, object]) -> dict[str, object]:
    return {
        key: (
            _REDACTED_VALIDATION_INPUT
            if key in _UNTRUSTED_VALIDATION_CONTEXT_KEYS
            else _sanitize_validation_context_value(value)
        )
        for key, value in context.items()
    }


def _safe_validation_location_segment(segment: str) -> bool:
    if len(segment) > 255 or any(ord(character) < 32 for character in segment):
        return False
    if (
        _SENSITIVE_VALIDATION_TEXT.search(segment) is not None
        or _EMAIL_LOCATION.search(segment) is not None
        or _PAYMENT_CARD_LOCATION.search(segment) is not None
    ):
        return False
    normalized = "".join(
        character for character in segment.casefold() if character.isalnum()
    )
    return not any(part in normalized for part in _SENSITIVE_LOCATION_PARTS)


def _sanitize_validation_location(
    location: tuple[int | str, ...],
    *,
    redact_dynamic_tail: bool = False,
) -> tuple[int | str, ...]:
    sanitized: list[int | str] = []
    for index, segment in enumerate(location):
        dynamic_tail = redact_dynamic_tail and index == len(location) - 1
        if isinstance(segment, int):
            sanitized.append(segment)
        elif dynamic_tail or not _safe_validation_location_segment(segment):
            sanitized.append(_REDACTED_VALIDATION_LOCATION)
        else:
            sanitized.append(segment)
    return tuple(sanitized)


def _redacted_validation_error(
    model_type: type[BaseModel] | str,
    error: ValidationError,
) -> ValidationError:
    line_errors: list[InitErrorDetails] = []
    for details in error.errors(include_url=False):
        redacted = InitErrorDetails(
            type=details["type"],
            loc=_sanitize_validation_location(
                details["loc"],
                redact_dynamic_tail=details["type"] == "extra_forbidden",
            ),
            input=_REDACTED_VALIDATION_INPUT,
        )
        context = details.get("ctx")
        if context is not None:
            redacted["ctx"] = _sanitize_validation_context(context)
        line_errors.append(redacted)
    return ValidationError.from_exception_data(
        model_type if isinstance(model_type, str) else model_type.__name__,
        line_errors,
        hide_input=True,
    )


def _canonical_data(
    value: CanonicalInput,
    *,
    exclude_top_level: frozenset[str] = frozenset(),
) -> object:
    if isinstance(value, BaseModel):
        dumped = cast(
            CanonicalValue,
            value.model_dump(
                mode="python",
                exclude=set(exclude_top_level),
                round_trip=True,
            ),
        )
        return _canonical_data(dumped)
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, float) and value == 0:
        return 0.0
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Канонический datetime должен содержать часовой пояс")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bytes):
        # Hex не зависит от алфавита base64 и однозначно сохраняет bytes.
        return {"encoding": "hex", "value": value.hex()}
    if isinstance(value, Mapping):
        return {
            key: _canonical_data(nested)
            for key, nested in sorted(value.items(), key=lambda item: item[0])
            if key not in exclude_top_level
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [_canonical_data(item) for item in value]
    return value


def canonical_json_value(
    value: CanonicalInput,
    *,
    exclude_top_level: frozenset[str] = frozenset(),
) -> str:
    """Сериализовать разрешённое значение в стабильное JSON-представление."""

    return json.dumps(
        _canonical_data(value, exclude_top_level=exclude_top_level),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_sha256_value(
    value: CanonicalInput,
    *,
    exclude_top_level: frozenset[str] = frozenset(),
) -> str:
    """Вычислить SHA-256 по каноническому JSON без self-reference полей."""

    payload = canonical_json_value(value, exclude_top_level=exclude_top_level)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class FrozenContract(BaseModel):
    """База для immutable DTO со строгим набором полей."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        hide_input_in_errors=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    def __init__(self, /, **data: object) -> None:
        try:
            super().__init__(**data)
        except ValidationError as error:
            raise _redacted_validation_error(type(self), error) from None

    def __setattr__(self, name: str, value: object) -> None:
        try:
            super().__setattr__(name, value)
        except ValidationError as error:
            raise _redacted_validation_error(type(self), error) from None

    @classmethod
    def model_validate(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        from_attributes: bool | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Проверить Python value без раскрытия отклонённого input в ошибке."""

        try:
            return super().model_validate(
                obj,
                strict=strict,
                extra=extra,
                from_attributes=from_attributes,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as error:
            raise _redacted_validation_error(cls, error) from None

    @classmethod
    def model_validate_json(
        cls,
        json_data: str | bytes | bytearray,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Проверить JSON без раскрытия отклонённого input в ошибке."""

        try:
            return super().model_validate_json(
                json_data,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as error:
            raise _redacted_validation_error(cls, error) from None

    @classmethod
    def model_validate_strings(
        cls,
        obj: object,
        *,
        strict: bool | None = None,
        extra: ExtraValues | None = None,
        context: object | None = None,
        by_alias: bool | None = None,
        by_name: bool | None = None,
    ) -> Self:
        """Проверить string mapping без раскрытия input в ошибке."""

        try:
            return super().model_validate_strings(
                obj,
                strict=strict,
                extra=extra,
                context=context,
                by_alias=by_alias,
                by_name=by_name,
            )
        except ValidationError as error:
            raise _redacted_validation_error(cls, error) from None

    def __eq__(self, other: object) -> bool:
        """Сравнивать полное значение только внутри точного DTO-типа."""

        if type(self) is not type(other):
            return False
        return super().__eq__(other)

    def canonical_json(self) -> str:
        """Вернуть детерминированный JSON полного значения DTO."""

        return canonical_json_value(self)
