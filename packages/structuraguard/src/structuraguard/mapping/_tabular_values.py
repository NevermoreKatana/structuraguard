"""Ограниченные признаки цельного значения без выбора семантической цели."""

import re
from datetime import date, time
from typing import Literal
from uuid import UUID

type AtomicValueKind = Literal["uuid", "date", "time", "decimal"]


def atomic_value_kind(value: str) -> AtomicValueKind | None:
    """Распознать полную закрытую grammar; пунктуация внутри не означает части.

    Признак описывает только форму значения. Например, UUID не доказывает,
    какой именно идентификатор хранится в колонке. Значение не преобразуется.
    """
    if len(value) > 128:
        return None
    if (
        re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value)
        and str(UUID(value)) == value.lower()
    ):
        return "uuid"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return None
        return "date"
    if re.fullmatch(
        r"[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?"
        r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?",
        value,
    ):
        try:
            time.fromisoformat(value)
        except ValueError:
            return None
        return "time"
    if re.fullmatch(r"[+-]?(?:0|[1-9][0-9]*)\.[0-9]+", value):
        return "decimal"
    return None
