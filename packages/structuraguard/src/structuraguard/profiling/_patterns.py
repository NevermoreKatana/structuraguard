"""Закрытые bounded recognizers; locale задан явно, содержимое не исполняется."""

import re
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from ipaddress import IPv6Address
from urllib.parse import urlsplit
from uuid import UUID

from structuraguard.contracts.common import (
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    NormalizedScalar,
)
from structuraguard.contracts.profiling import (
    LocalePolicy,
    PatternCode,
    PIICategory,
    ProfileReason,
    TypeKind,
)

_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
    re.ASCII,
)
_RU_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:,[0-9]+)?", re.ASCII
)
_EN_NUMBER = re.compile(
    r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?", re.ASCII
)
_CURRENCY = re.compile(r"(RUB|USD|EUR|GBP|₽|\$|€|£|руб\.?)", re.IGNORECASE)
_PHONE = re.compile(
    r"\+?[0-9 ()-]+(?:\s*(?:ext\.?|доб\.?)\s*[0-9]{1,6})?", re.ASCII | re.IGNORECASE
)
_CREDENTIAL = re.compile(
    r"(?i)(?:password|passwd|api[_-]?key|secret|token|пароль)\s*[:=]\s*\S+"
)


@dataclass
class Scan:
    patterns: set[PatternCode] = field(default_factory=set)
    candidates: set[TypeKind] = field(default_factory=set)
    parsed: list[NormalizedScalar] = field(default_factory=list)
    reasons: set[ProfileReason] = field(default_factory=set)
    categories: set[PIICategory] = field(default_factory=set)
    currencies: set[str] = field(default_factory=set)


def _inn(text: str) -> PatternCode | None:
    if (
        not text.isascii()
        or not text.isdigit()
        or len(text) not in (10, 12)
        or not int(text)
    ):
        return None
    digits = [int(c) for c in text]
    if len(digits) == 10:
        check = (
            sum(
                a * b
                for a, b in zip(digits, (2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
            )
            % 11
            % 10
        )
        return "russian_inn_10" if check == digits[9] else None
    first = (
        sum(
            a * b for a, b in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
        )
        % 11
        % 10
    )
    second = (
        sum(
            a * b
            for a, b in zip(digits, (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=False)
        )
        % 11
        % 10
    )
    return "russian_inn_12" if (first, second) == (digits[10], digits[11]) else None


def _numbers(text: str, locale: LocalePolicy) -> set[Decimal]:
    values: set[Decimal] = set()
    if locale in (
        LocalePolicy.UNSPECIFIED,
        LocalePolicy.RU_RU,
    ) and _RU_NUMBER.fullmatch(text):
        values.add(
            Decimal(
                text.replace(" ", "")
                .replace("\u00a0", "")
                .replace("\u202f", "")
                .replace(",", ".")
            )
        )
    if locale != LocalePolicy.RU_RU and _EN_NUMBER.fullmatch(text):
        values.add(Decimal(text.replace(",", "")))
    return values


def _dates(text: str, locale: LocalePolicy, result: Scan) -> None:
    dates: set[date] = set()
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        with suppress(ValueError):
            dates.add(date.fromisoformat(text))
    if locale in (LocalePolicy.UNSPECIFIED, LocalePolicy.RU_RU) and re.fullmatch(
        r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", text
    ):
        day, month, year = (int(p) for p in text.split("."))
        with suppress(ValueError):
            dates.add(date(year, month, day))
    if re.fullmatch(r"[0-9]{2}/[0-9]{2}/[0-9]{4}", text):
        first, second, year = (int(p) for p in text.split("/"))
        pairs: tuple[tuple[int, int], ...] = (
            ((first, second),) if locale == LocalePolicy.EN_US else ((second, first),)
        )
        if locale == LocalePolicy.UNSPECIFIED:
            pairs = ((first, second), (second, first))
        if locale == LocalePolicy.RU_RU:
            pairs = ()
        for month, day in pairs:
            with suppress(ValueError):
                dates.add(date(year, month, day))
    if dates:
        result.patterns.add("date")
        result.candidates.add("date")
        if len(dates) > 1:
            result.reasons.add("locale_ambiguity")
        else:
            result.parsed.append(DateScalar(value=next(iter(dates))))
    if re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?",
        text,
    ):
        try:
            # fromisoformat() переносит лишние минуты offset в часы без ошибки.
            timestamp = datetime.fromisoformat(text)
            utc_value = (
                DateTimeScalar(value=timestamp)
                if timestamp.tzinfo is not None
                else None
            )
        except (ValueError, OverflowError):
            # Локальная дата на границе 1/9999 года может выйти за диапазон в UTC.
            return
        result.patterns.add("datetime")
        result.candidates.add("datetime")
        if utc_value is None:
            result.reasons.add("naive_datetime")
        else:
            result.parsed.append(utc_value)


def _valid_host(host: str) -> bool:
    if ":" in host:
        try:
            IPv6Address(host)
        except ValueError:
            return False
        return True
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_host) > 253:
        return False
    return all(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in ascii_host.rstrip(".").split(".")
    )


def scan_string(text: str, locale: LocalePolicy, *, money_hint: bool) -> Scan:
    """Проанализировать заранее ограниченную строку без изменения исходных bytes."""
    result = Scan()
    if any(
        _EMAIL.fullmatch(token.strip("<>(),;"))
        for token in text.split()
        if "@" in token
    ):
        result.categories.add("email")
    if _CREDENTIAL.search(text):
        result.categories.add("credential")
    # Опечатка домена не снимает PII-сигнал выше; для type/identity нужна DNS-форма.
    if _EMAIL.fullmatch(text) and _valid_host(text.rpartition("@")[2]):
        result.patterns.add("email")
        result.candidates.add("identifier")
    # Проверяем grammar cap до regex с пересекающимися whitespace-квантификаторами.
    if len(text) <= 64 and _PHONE.fullmatch(text):
        core = re.split(r"(?i)ext\.?|доб\.?", text)[0]
        digits = sum(c.isascii() and c.isdigit() for c in core)
        if (10 <= digits <= 15) or (core.startswith("+") and 7 <= digits <= 15):
            result.patterns.add("phone")
            result.candidates.add("identifier")
            result.categories.add("phone")
    inn = _inn(text)
    if inn:
        result.patterns.add(inn)
        result.candidates.add("identifier")
        if inn == "russian_inn_12":
            result.categories.add("personal_tax_id")
        # Tax ID не классифицируется как телефон только по числу цифр.
        result.patterns.discard("phone")
        result.categories.discard("phone")
    if len(text) in (32, 36):
        try:
            identifier = UUID(text)
        except ValueError:
            pass
        else:
            if text.lower() in (str(identifier), identifier.hex):
                result.patterns.add("uuid")
                result.candidates.add("identifier")
    if text.startswith(("https://", "http://")) and not any(
        c.isspace() or ord(c) < 32 for c in text
    ):
        try:
            url = urlsplit(text)
            port = url.port
            host = url.hostname
        except ValueError:
            pass
        else:
            if host and _valid_host(host) and (port is None or 0 < port < 65536):
                result.patterns.add("url")
                result.candidates.add("identifier")
                if url.username is not None or url.password is not None:
                    result.categories.add("credential")
    normalized = text.strip()
    matches = tuple(_CURRENCY.finditer(normalized))
    symbols = [match.group(0) for match in matches]
    numeric_text = normalized
    currency_valid = not matches or (
        len(matches) == 1
        and (matches[0].start() == 0 or matches[0].end() == len(normalized))
    )
    if matches and currency_valid:
        match = matches[0]
        numeric_text = (normalized[: match.start()] + normalized[match.end() :]).strip()
    numbers = _numbers(numeric_text, locale) if currency_valid else set()
    leading_zero = bool(re.fullmatch(r"[+-]?0[0-9]+", numeric_text))
    if numbers:
        result.patterns.add("decimal")
        result.candidates.add("decimal")
        if len(numbers) > 1:
            result.reasons.add("locale_ambiguity")
        else:
            number = next(iter(numbers))
            result.parsed.append(DecimalScalar(value=number))
        if re.fullmatch(r"[+-]?[0-9]+", numeric_text) and not symbols:
            result.patterns.add("integer")
            result.candidates.discard("decimal")
            result.candidates.add("integer")
        if leading_zero or inn:
            result.candidates.discard("integer")
            result.candidates.discard("decimal")
            result.candidates.add("identifier")
        if symbols:
            result.patterns.add("currency")
            result.currencies.update(symbol.upper() for symbol in symbols)
            if len(set(symbols)) > 1 or "$" in symbols:
                result.reasons.add("currency_ambiguity")
        if symbols or money_hint:
            result.patterns.add("money")
            result.candidates.discard("integer")
            result.candidates.discard("decimal")
            result.candidates.add("money")
            result.categories.add("financial")
    if normalized.casefold() in {"true", "false", "yes", "no", "да", "нет", "0", "1"}:
        result.patterns.add("boolean")
        result.candidates.add("boolean")
    _dates(normalized, locale, result)
    if not result.patterns and len(text) >= 32:
        result.patterns.add("free_text")
    return result
