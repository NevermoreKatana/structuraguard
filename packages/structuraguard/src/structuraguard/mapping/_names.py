"""Ограниченные лексические сигналы без locale, NLP и I/O."""

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from structuraguard.exceptions import MappingError

from ._scores import product, ratio

GENERIC = frozenset(
    {
        "id",
        "name",
        "date",
        "code",
        "value",
        "имя",
        "дата",
        "код",
        "значение",
        "row",
        "entity",
    }
)
_RUSSIAN = "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
_LATIN = (
    "a",
    "b",
    "v",
    "g",
    "d",
    "e",
    "yo",
    "zh",
    "z",
    "i",
    "y",
    "k",
    "l",
    "m",
    "n",
    "o",
    "p",
    "r",
    "s",
    "t",
    "u",
    "f",
    "kh",
    "ts",
    "ch",
    "sh",
    "shch",
    "",
    "y",
    "",
    "e",
    "yu",
    "ya",
)


@dataclass(frozen=True, repr=False)
class Name:
    raw: str
    tokens: tuple[str, ...]
    compact: str
    transliterated: tuple[str, ...]
    folded_transliterated: tuple[str, ...]
    confusable: bool

    @property
    def generic(self) -> bool:
        return not self.tokens or all(t in GENERIC for t in self.tokens)


@dataclass(frozen=True)
class NameEvidence:
    exact: Decimal = Decimal(0)
    normalized: Decimal = Decimal(0)
    compact: Decimal = Decimal(0)
    token: Decimal = Decimal(0)
    transliterated: Decimal = Decimal(0)
    edit: Decimal = Decimal(0)

    @property
    def score(self) -> Decimal:
        return max(
            self.exact,
            self.normalized,
            self.compact,
            self.token,
            self.transliterated,
            self.edit,
        )


def _transliterate(token: str) -> str:
    return "".join(_LATIN[_RUSSIAN.index(c)] if c in _RUSSIAN else c for c in token)


def normalize_name(value: str, *, max_bytes: int = 256, max_tokens: int = 32) -> Name:
    """Сохранить spelling отдельно от matching forms; overflow не обрезается."""
    if len(value) > max_bytes or len(value.encode("utf-8")) > max_bytes:
        raise MappingError(
            error_code="MAPPING_LIMIT_EXCEEDED", message="Превышен лимит имени."
        )
    text = unicodedata.normalize("NFKC", value)
    text = re.sub(r"([A-ZА-ЯЁ]+)([A-ZА-ЯЁ][a-zа-яё])", r"\1 \2", text)
    text = re.sub(r"([a-zа-яё])([A-ZА-ЯЁ])", r"\1 \2", text).casefold()
    parts: list[str] = []
    pending = ""
    for char in text:
        if not char.isalnum():
            if pending:
                parts.append(pending)
                pending = ""
        else:
            if pending and pending[-1].isdigit() != char.isdigit():
                parts.append(pending)
                pending = ""
            pending += char
    if pending:
        parts.append(pending)
    if len(parts) > max_tokens:
        raise MappingError(
            error_code="MAPPING_LIMIT_EXCEEDED", message="Превышен лимит tokens."
        )
    tokens = tuple(parts)
    return Name(
        raw=value,
        tokens=tokens,
        compact="".join(tokens),
        transliterated=tuple(_transliterate(t) for t in tokens),
        folded_transliterated=tuple(
            _transliterate(t.replace("ё", "е")) for t in tokens
        ),
        confusable=any(
            any("a" <= c <= "z" for c in t) and any(c in _RUSSIAN for c in t)
            for t in tokens
        ),
    )


def token_dice(left: tuple[str, ...], right: tuple[str, ...]) -> Decimal:
    a, b = set(left), set(right)

    def weight(items: set[str]) -> int:
        return sum(1 if t in GENERIC else 4 for t in items)

    return ratio(2 * weight(a & b), weight(a) + weight(b))


def _bounded_edit_distance(left: str, right: str, limit: int) -> int:
    """Вставки, удаления, замены и соседние перестановки: O(length × limit)."""
    overflow = limit + 1
    if abs(len(left) - len(right)) > limit:
        return overflow
    previous = {j: j for j in range(min(len(right), limit) + 1)}
    before_previous: dict[int, int] = {}
    for i, char in enumerate(left, 1):
        current = {0: i} if i <= limit else {}
        for j in range(max(1, i - limit), min(len(right), i + limit) + 1):
            value = min(
                previous.get(j, overflow) + 1,
                current.get(j - 1, overflow) + 1,
                previous.get(j - 1, overflow) + (char != right[j - 1]),
            )
            if i > 1 and j > 1 and char == right[j - 2] and left[i - 2] == right[j - 1]:
                value = min(value, before_previous.get(j - 2, overflow) + 1)
            current[j] = min(value, overflow)
        if min(current.values(), default=overflow) > limit:
            return overflow
        before_previous, previous = previous, current
    return previous.get(len(right), overflow)


def _edit_similarity(left: Name, right: Name) -> Decimal:
    """Опечатка предлагает кандидата, но слабее точного имени и не стирает цифры."""
    if (
        left.compact == right.compact
        or min(len(left.compact), len(right.compact)) < 4
        or left.generic
        or right.generic
        or left.confusable
        or right.confusable
        or tuple(t for t in left.tokens if t.isdigit())
        != tuple(t for t in right.tokens if t.isdigit())
    ):
        return Decimal(0)
    length = max(len(left.compact), len(right.compact))
    limit = min(3, length // 4)
    distance = _bounded_edit_distance(left.compact, right.compact, limit)
    if distance > limit:
        return Decimal(0)
    return product(ratio(length - distance, length), Decimal("0.90"))


def compare_names(left: Name, right: Name) -> NameEvidence:
    """Raw, normalized и transliteration видны отдельно от выбранного max."""
    if not left.tokens or not right.tokens:
        return NameEvidence()
    transliteration = Decimal(0)
    if left.transliterated != left.tokens or right.transliterated != right.tokens:
        for a in (left.transliterated, left.folded_transliterated):
            for b in (right.transliterated, right.folded_transliterated):
                if not "".join(a) or not "".join(b):
                    continue
                value = (
                    Decimal("0.80")
                    if "".join(a) == "".join(b)
                    else product(token_dice(a, b), Decimal("0.75"))
                )
                transliteration = max(transliteration, value)
    return NameEvidence(
        exact=Decimal(int(left.raw == right.raw)),
        normalized=Decimal("0.98") if left.tokens == right.tokens else Decimal(0),
        compact=Decimal("0.95") if left.compact == right.compact else Decimal(0),
        token=product(token_dice(left.tokens, right.tokens), Decimal("0.90")),
        transliterated=transliteration,
        edit=_edit_similarity(left, right),
    )


def best_names(sources: tuple[Name, ...], target: Name) -> NameEvidence:
    evidence = tuple(compare_names(source, target) for source in sources)
    return NameEvidence(
        exact=max((e.exact for e in evidence), default=Decimal(0)),
        normalized=max((e.normalized for e in evidence), default=Decimal(0)),
        compact=max((e.compact for e in evidence), default=Decimal(0)),
        token=max((e.token for e in evidence), default=Decimal(0)),
        transliterated=max((e.transliterated for e in evidence), default=Decimal(0)),
        edit=max((e.edit for e in evidence), default=Decimal(0)),
    )
