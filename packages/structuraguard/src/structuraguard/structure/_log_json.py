"""Строгое bounded чтение scalar из JSON сообщения syslog, без expressions."""

import json
from typing import cast

from structuraguard.contracts.common import (
    BooleanScalar,
    NormalizedScalar,
    NullScalar,
    StringScalar,
)
from structuraguard.contracts.execution import ExecutionStage, ParsePlanOptions
from structuraguard.parsers.builtin._log_detection import ISO_SYSLOG_EVENT
from structuraguard.structure._plan_check import failure

type JsonValue = str | bool | list[JsonValue] | dict[str, JsonValue] | None


def _reject_constant(_value: str) -> None:
    raise ValueError


def _object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _limit() -> None:
    raise failure(
        "log_json_budget", code="SECURITY_LIMIT_EXCEEDED", stage=ExecutionStage.LIMIT
    )


def decode_log_json(text: str, options: ParsePlanOptions) -> dict[str, JsonValue]:
    """Проверить envelope, бюджеты и единственный JSON object; не восстанавливать JSON."""

    envelope = ISO_SYSLOG_EVENT.match(text)
    if envelope is None:
        raise failure("log_json_envelope_missing")
    payload = text[envelope.end() :]
    if len(payload) * 4 > options.max_record_bytes:
        _limit()
    depth = separators = 0
    quoted = escaped = False
    for character in payload:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                _limit()
        elif character in "]}":
            depth -= 1
        elif character in ",:":
            separators += 1
            if separators > options.max_record_items * 2:
                _limit()
    try:
        parsed: object = json.loads(
            payload,
            parse_int=str,
            parse_float=str,
            parse_constant=_reject_constant,
            object_pairs_hook=_object,
        )
    except (ValueError, RecursionError):
        raise failure("log_json_malformed") from None
    if type(parsed) is not dict:
        raise failure("log_json_object_required")
    stack: list[object] = [parsed]
    items = 0
    while stack:
        item = stack.pop()
        items += 1
        if items > options.max_record_items:
            _limit()
        if type(item) is dict:
            if len(item) * 2 + len(stack) + items > options.max_record_items:
                _limit()
            stack.extend(item.keys())
            stack.extend(item.values())
        elif type(item) is list:
            if len(item) + len(stack) + items > options.max_record_items:
                _limit()
            stack.extend(item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError:
                raise failure("log_json_malformed") from None
    return cast(dict[str, JsonValue], parsed)


def select_log_json(
    payload: dict[str, JsonValue], path: tuple[str, ...]
) -> NormalizedScalar:
    """Пройти только буквальные object keys; missing не превращается в null."""

    selected: JsonValue = payload
    for key in path:
        if not isinstance(selected, dict) or key not in selected:
            raise failure("log_json_path_missing")
        selected = selected[key]
    if selected is None:
        return NullScalar()
    if isinstance(selected, bool):
        return BooleanScalar(value=selected)
    if isinstance(selected, str):
        return StringScalar(value=selected)
    raise failure("log_json_scalar_required")
