"""Bounded builtin recognizers и существующая safe ASCII regex grammar."""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from structuraguard.contracts.privacy import DetectionPolicy
from structuraguard.contracts.privacy import SensitiveCategory as Category
from structuraguard.domain.bounded_regex import safe_pattern
from structuraguard.domain.pii_patterns import card_like, russian_inn

from ._privacy import failure


@dataclass(frozen=True, slots=True)
class Pattern:
    category: Category
    regex: re.Pattern[str]
    cost: int


# Конечные widths: builtin re не получает произвольные пользовательские выражения.
BUILTINS = (
    Pattern(
        Category.EMAIL,
        re.compile(
            r"(?<![A-Za-z0-9.!#$%&'*+/=?^_`{|}~-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9][A-Za-z0-9.-]{0,252}\.[A-Za-z]{2,63}(?![A-Za-z0-9.-])",
            re.ASCII,
        ),
        512,
    ),
    Pattern(
        Category.PHONE,
        re.compile(
            r"(?<![A-Za-z0-9])\+?[0-9](?:[ ()-]{0,3}[0-9]){6,14}(?![0-9])", re.ASCII
        ),
        128,
    ),
    Pattern(
        Category.INN,
        re.compile(r"(?<![0-9])[0-9]{10}(?:[0-9]{2})?(?![0-9])", re.ASCII),
        16,
    ),
    Pattern(
        Category.SNILS,
        re.compile(
            r"(?<![0-9])[0-9]{3}[- ]?[0-9]{3}[- ]?[0-9]{3}[ -]?[0-9]{2}(?![0-9])",
            re.ASCII,
        ),
        16,
    ),
    Pattern(
        Category.PASSPORT,
        re.compile(
            r"(?<![0-9])[0-9]{2}[ ]?[0-9]{2}[ ]{1,3}[0-9]{6}(?![0-9])", re.ASCII
        ),
        16,
    ),
    Pattern(
        Category.CARD,
        re.compile(r"(?<![0-9])(?:[0-9][ -]?){12,18}[0-9](?![0-9])", re.ASCII),
        40,
    ),
)
_PRIVATE = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|-----BEGIN PGP PRIVATE KEY BLOCK-----"
)
_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w])(?:password(?:hash)?|passwd|pwd|api[_-]?key|secret|(?:access[_-]?|refresh[_-]?|auth[_-]?)token|authorization|пароль)[\"']?[ \t]{0,8}[:=][ \t]{0,8}"
)
_BEARER = re.compile(r"(?i)(?<![\w])bearer[ \t]{1,8}")
_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,31}://", re.ASCII)
_WORD = re.compile(r"[A-Za-z0-9_./+=$-]+", re.ASCII)


def custom_pattern(pattern: str) -> re.Pattern[str]:
    if not safe_pattern(pattern, 256):
        raise failure("SECURITY_PATTERN_UNSUPPORTED") from None
    compiled = re.compile(pattern, re.ASCII)
    if compiled.search("") is not None:
        raise failure("SECURITY_PATTERN_UNSUPPORTED") from None
    return compiled


def accepted(category: Category, text: str, policy: DetectionPolicy) -> bool:
    if category is Category.CARD:
        return card_like(text)
    if category is Category.INN:
        return policy.inn_validation == "pattern" or russian_inn(text) is not None
    if category is Category.PHONE:
        digits = sum("0" <= c <= "9" for c in text)
        return (
            text.startswith("+")
            if policy.phone_mode == "international"
            else digits >= 10 or text.startswith("+")
        )
    return True


def field_category(name: str, custom_fields: tuple[str, ...]) -> Category | None:
    normalized = "".join(c for c in name.casefold() if c.isalnum())
    if normalized in custom_fields:
        return Category.PASSWORD
    if (
        any(word in normalized for word in ("password", "passwd", "пароль", "secret"))
        or normalized == "pwd"
    ):
        return Category.PASSWORD
    if any(
        word in normalized
        for word in ("apikey", "privatekey", "connectionstring", "dsn")
    ):
        return Category.PRIVATE_KEY if "privatekey" in normalized else Category.API_KEY
    if "token" in normalized or normalized in {"authorization", "cookie", "setcookie"}:
        return Category.TOKEN
    if normalized in {"fullname", "firstname", "lastname", "фио", "фамилия", "имя"}:
        return Category.PERSON_NAME
    if normalized in {"passport", "паспорт", "passportnumber"}:
        return Category.PASSPORT
    for category, labels in (
        (Category.EMAIL, {"email", "emailaddress", "электроннаяпочта"}),
        (Category.PHONE, {"phone", "phonenumber", "telephone", "телефон"}),
        (Category.INN, {"inn", "инн"}),
        (Category.SNILS, {"snils", "снилс"}),
        (Category.CARD, {"cardnumber", "creditcard", "pan", "номеркарты"}),
    ):
        if normalized in labels:
            return category
    return None


def secret_spans(text: str) -> Iterator[tuple[int, int, Category]]:
    position = 0
    while (opening := _PRIVATE.search(text, position)) is not None:
        closing = opening.group().replace("BEGIN", "END", 1)
        end = text.find(closing, opening.end())
        position = len(text) if end < 0 else end + len(closing)
        yield opening.start(), position, Category.PRIVATE_KEY
    for pattern in (_ASSIGNMENT, _BEARER):
        consumed = 0
        for match in pattern.finditer(text):
            if match.start() < consumed:
                continue
            start = match.end()
            if start == len(text):
                continue
            if text[start] in "\"'":
                quote = text[start]
                start += 1
                end = start
                while end < len(text) and text[end] != quote:
                    end += 2 if text[end] == "\\" else 1
                end = min(end, len(text))
            else:
                end = start
                while end < len(text) and text[end] not in " \t\r\n,;":
                    end += 1
            consumed = end
            if end > start:
                kind = (
                    Category.TOKEN
                    if pattern is _BEARER
                    else field_category(match.group(), ()) or Category.PASSWORD
                )
                yield start, end, kind
    consumed = 0
    for match in _URI.finditer(text):
        if match.start() < consumed:
            continue
        end = match.end()
        while end < len(text) and text[end] not in " \t\r\n\"'<>;":
            end += 1
        consumed = end
        authority = text[match.end() : end]
        for separator in "/?#":
            authority = authority.split(separator, 1)[0]
        if "@" in authority:
            yield match.start(), end, Category.API_KEY
    for match in _WORD.finditer(text):
        word = match.group()
        if (
            word.startswith(
                (
                    "sk-",
                    "ghp_",
                    "gho_",
                    "ghu_",
                    "ghs_",
                    "ghr_",
                    "github_pat_",
                    "AKIA",
                    "ASIA",
                    "xoxb-",
                    "xoxp-",
                    "xoxa-",
                    "AIza",
                )
            )
            and len(word) >= 20
        ):
            yield match.start(), match.end(), Category.API_KEY
        elif (
            word.startswith("eyJ")
            and word.count(".") == 2
            and all(len(p) >= 4 for p in word.split("."))
        ):
            yield match.start(), match.end(), Category.TOKEN
