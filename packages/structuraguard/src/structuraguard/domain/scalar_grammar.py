"""Общие закрытые locale grammars для inference M8 и pure normalizers.

Helpers не выбирают один вариант при неоднозначности. Caller ограничивает длину
строк/цифр до вызова; никакие environment/process locale здесь не читаются.
"""

import re
from datetime import date
from decimal import Decimal

from structuraguard.contracts.profiling import LocalePolicy

_RU_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:,[0-9]+)?", re.ASCII
)
_EN_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", re.ASCII
)


def decimal_text_candidates(text: str, locale: LocalePolicy) -> tuple[str, ...]:
    """Вернуть canonical decimal тексты всех допустимых locale interpretations."""
    candidates: list[str] = []
    if locale in (
        LocalePolicy.UNSPECIFIED,
        LocalePolicy.RU_RU,
    ) and _RU_NUMBER.fullmatch(text):
        candidates.append(
            text.replace(" ", "")
            .replace("\u00a0", "")
            .replace("\u202f", "")
            .replace(",", ".")
        )
    if locale != LocalePolicy.RU_RU and _EN_NUMBER.fullmatch(text):
        candidate = text.replace(",", "")
        if candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def decimal_candidates(text: str, locale: LocalePolicy) -> set[Decimal]:
    """Сохранить исходную M8 семантику множества возможных точных чисел."""
    return {Decimal(candidate) for candidate in decimal_text_candidates(text, locale)}


def date_candidates(text: str, locale: LocalePolicy) -> set[date]:
    """Вернуть ISO/locale calendar dates без выбора между DMY и MDY."""
    triples: list[tuple[int, int, int]] = []
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        year, month, day = (int(part) for part in text.split("-"))
        triples.append((year, month, day))
    if locale in (LocalePolicy.UNSPECIFIED, LocalePolicy.RU_RU) and re.fullmatch(
        r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", text
    ):
        day, month, year = (int(part) for part in text.split("."))
        triples.append((year, month, day))
    if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", text):
        first, second, year = (int(part) for part in text.split("/"))
        if locale in (LocalePolicy.EN_US, LocalePolicy.UNSPECIFIED):
            triples.append((year, first, second))
        if locale in (LocalePolicy.EN_GB, LocalePolicy.UNSPECIFIED):
            triples.append((year, second, first))
    result: set[date] = set()
    for triple in triples:
        try:
            result.add(date(*triple))
        except ValueError:
            continue
    return result
