"""Публичные типизированные ошибки StructuraGuard."""

import math
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

type ErrorDetailScalar = str | int | float | bool | None
type ErrorDetailInput = (
    ErrorDetailScalar
    | Mapping[str, ErrorDetailInput]
    | list[ErrorDetailInput]
    | tuple[ErrorDetailInput, ...]
)
type ErrorDetail = (
    ErrorDetailScalar | Mapping[str, ErrorDetail] | tuple[ErrorDetail, ...]
)

_REDACTED: Final = "[REDACTED]"
_NON_FINITE: Final = "[NON_FINITE_NUMBER]"
_DETAIL_LIMIT: Final = "[DETAIL_LIMIT_EXCEEDED]"
_TRUNCATED: Final = "[TRUNCATED]"
_MAX_DETAIL_DEPTH: Final = 16
_MAX_DETAIL_ITEMS: Final = 256
_MAX_TEXT_CHARS: Final = 4_096
_ERROR_CODE_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9_]*$")
_URI_SCHEME_PATTERN: Final = re.compile(r"(?i)[a-z][a-z0-9+.-]*://")
_URI_AUTHORITY_DELIMITER_PATTERN: Final = re.compile(r"[/ ?#\t\r\n]")
_SENSITIVE_KEY_PARTS: Final = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "credential",
    "dsn",
    "privatekey",
    "cookie",
)
_TEXT_REDACTIONS: Final = (
    (
        re.compile(
            r"(?is)-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----.*?"
            r"(?:-----END(?: [A-Z0-9]+)* PRIVATE KEY-----|$)"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/?#@\s]+@"),
        r"\1[REDACTED]@",
    ),
    (
        re.compile(r"(?im)\b((?:proxy-)?authorization)(\s*[:=]\s*)[^\r\n]*"),
        r"\1\2[REDACTED]",
    ),
    (
        re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+"),
        r"\1 [REDACTED]",
    ),
    (
        re.compile(r"(?im)\b(set-cookie|cookie)\s*:\s*[^\r\n]*"),
        r"\1: [REDACTED]",
    ),
    (
        re.compile(
            r"(?ix)"
            r"(?P<quote>['\"]?)"
            r"(?P<key>[a-z0-9_.-]*"
            r"(?:password|passwd|secret|token|api[_-]?key|private[_-]?key|"
            r"authorization|credential|dsn|set[_-]?cookie|cookie)"
            r"[a-z0-9_.-]*)"
            r"(?P=quote)\s*[:=]\s*"
            r"(?!\[REDACTED\])"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
        ),
        r"\g<quote>\g<key>\g<quote>=[REDACTED]",
    ),
)


def _redact_unresolved_truncated_uri_authority(candidate: str) -> str:
    for scheme in reversed(tuple(_URI_SCHEME_PATTERN.finditer(candidate))):
        authority_start = scheme.end()
        visible_authority = candidate[authority_start:]
        if _URI_AUTHORITY_DELIMITER_PATTERN.search(visible_authority) is not None:
            continue
        return f"{candidate[:authority_start]}{_REDACTED}"
    return candidate


class _SanitizationState:
    __slots__ = ("active_container_ids", "remaining_items")

    def __init__(self) -> None:
        self.active_container_ids: set[int] = set()
        self.remaining_items = _MAX_DETAIL_ITEMS

    def consume(self) -> bool:
        if self.remaining_items == 0:
            return False
        self.remaining_items -= 1
        return True


def _normalize_key(key: str) -> str:
    return "".join(character for character in key.casefold() if character.isalnum())


def _is_sensitive_key(key: str) -> bool:
    if len(key) > _MAX_TEXT_CHARS:
        return True
    normalized = _normalize_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _sanitize_text(value: str) -> str:
    candidate = value[:_MAX_TEXT_CHARS]
    if len(value) > _MAX_TEXT_CHARS:
        candidate = _redact_unresolved_truncated_uri_authority(candidate)
    for pattern, replacement in _TEXT_REDACTIONS:
        candidate = pattern.sub(replacement, candidate)
    if len(value) > _MAX_TEXT_CHARS:
        return f"{candidate}{_TRUNCATED}"
    return candidate


def _sanitize_detail_key(key: str) -> str:
    sanitized = _sanitize_text(key)
    maximum_length = _MAX_TEXT_CHARS + len(_TRUNCATED)
    if len(sanitized) <= maximum_length:
        return sanitized
    return f"{sanitized[:_MAX_TEXT_CHARS]}{_TRUNCATED}"


def _deduplicate_detail_key(
    key: str,
    existing: Mapping[str, ErrorDetail],
) -> str:
    if key not in existing:
        return key

    maximum_length = _MAX_TEXT_CHARS + len(_TRUNCATED)
    collision_number = 2
    while True:
        suffix = f"#{collision_number}"
        if key.endswith(_TRUNCATED):
            prefix_length = maximum_length - len(_TRUNCATED) - len(suffix)
            candidate = f"{key[:prefix_length]}{suffix}{_TRUNCATED}"
        else:
            candidate = f"{key[: maximum_length - len(suffix)]}{suffix}"
        if candidate not in existing:
            return candidate
        collision_number += 1


def _sanitize_detail(
    key: str,
    value: ErrorDetailInput,
    *,
    state: _SanitizationState,
    depth: int,
) -> ErrorDetail:
    if not state.consume():
        return _DETAIL_LIMIT
    if _is_sensitive_key(key):
        return _REDACTED
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _NON_FINITE
    if isinstance(value, str):
        return _sanitize_text(value)
    if depth >= _MAX_DETAIL_DEPTH:
        return _DETAIL_LIMIT

    container_id = id(value)
    if container_id in state.active_container_ids:
        return _DETAIL_LIMIT
    state.active_container_ids.add(container_id)
    try:
        if isinstance(value, Mapping):
            sanitized: dict[str, ErrorDetail] = {}
            for nested_key, nested_value in value.items():
                if state.remaining_items == 0:
                    truncated_key = _deduplicate_detail_key("_truncated", sanitized)
                    sanitized[truncated_key] = _DETAIL_LIMIT
                    break
                sanitized_key = _deduplicate_detail_key(
                    _sanitize_detail_key(nested_key),
                    sanitized,
                )
                sanitized[sanitized_key] = _sanitize_detail(
                    nested_key,
                    nested_value,
                    state=state,
                    depth=depth + 1,
                )
            return MappingProxyType(sanitized)

        sanitized_items: list[ErrorDetail] = []
        for item in value:
            if state.remaining_items == 0:
                sanitized_items.append(_DETAIL_LIMIT)
                break
            sanitized_items.append(
                _sanitize_detail("", item, state=state, depth=depth + 1)
            )
        return tuple(sanitized_items)
    finally:
        state.active_container_ids.remove(container_id)


def _sanitize_details(
    details: Mapping[str, ErrorDetailInput],
) -> Mapping[str, ErrorDetail]:
    sanitized = _sanitize_detail(
        "",
        details,
        state=_SanitizationState(),
        depth=0,
    )
    if not isinstance(sanitized, Mapping):
        return MappingProxyType({"_details": sanitized})
    return sanitized


class StructuraGuardError(Exception):
    """Базовая типизированная ошибка SDK.

    Args:
        error_code: Machine-readable код из заглавных латинских букв, цифр
            и символа ``_``.
        message: Безопасное описание ошибки для пользователя.
        details: Необязательные machine-readable детали.
        run_id: Необязательный идентификатор запуска.
        retryable: Допустим ли повтор операции.
        cause: Исходное исключение; сохраняется только имя его типа.

    Raises:
        ValueError: Если ``error_code`` не соответствует публичному формату.

    ``message``, ``run_id``, имя ``cause`` и строки в ``details`` ограничиваются
    по размеру и очищаются от распространённых секретов и учётных данных.
    ``details`` рекурсивно копируются в неизменяемую структуру; циклы и
    превышение лимитов заменяются безопасными маркерами. Неявный exception
    context скрывается из стандартного traceback. Маскирование служит
    дополнительной защитой и не отменяет запрет передавать произвольные секреты
    в ошибки или явно связывать с ними небезопасный ``cause``.
    """

    error_code: str
    message: str
    details: Mapping[str, ErrorDetail]
    run_id: str | None
    retryable: bool
    cause: str | None

    def __init__(
        self,
        *,
        error_code: str,
        message: str,
        details: Mapping[str, ErrorDetailInput] | None = None,
        run_id: str | None = None,
        retryable: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        if _ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError(
                "error_code должен состоять из заглавных латинских букв, цифр и '_'"
            )

        self.error_code = error_code
        self.message = _sanitize_text(message)
        self.details = _sanitize_details(details or {})
        self.run_id = _sanitize_text(run_id) if run_id is not None else None
        self.retryable = retryable
        self.cause = _sanitize_text(type(cause).__name__) if cause is not None else None
        super().__init__(f"{self.error_code}: {self.message}")
        self.__suppress_context__ = True


class SourceError(StructuraGuardError):
    """Ошибка получения или чтения источника."""


class ParserError(StructuraGuardError):
    """Ошибка определения формата или разбора источника."""


class DatabaseInspectionError(StructuraGuardError):
    """Ошибка безопасной инспекции целевой БД."""


class MappingError(StructuraGuardError):
    """Ошибка построения декларативного сопоставления."""


class ValidationError(StructuraGuardError):
    """Ошибка проверки данных или плана."""


class SecurityPolicyError(StructuraGuardError):
    """Отказ обязательной security policy."""


class LoadError(StructuraGuardError):
    """Ошибка staging или транзакционной загрузки."""


class OperationNotImplementedError(StructuraGuardError):
    """Ошибка вызова операции, которая ещё не поставляется в M1.

    Args:
        operation: Каноническое имя недоступной операции.

    Ошибка имеет ``error_code="SDK_OPERATION_NOT_IMPLEMENTED"``, сохраняет
    имя в ``details["operation"]`` и всегда имеет ``retryable=False``. Она не
    обозначает заглушку с фиктивным успехом.
    """

    def __init__(self, operation: str) -> None:
        super().__init__(
            error_code="SDK_OPERATION_NOT_IMPLEMENTED",
            message="Операция SDK ещё не реализована.",
            details={"operation": operation},
            retryable=False,
        )
