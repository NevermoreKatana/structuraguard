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

    @property
    def score(self) -> Decimal:
        return max(
            self.exact, self.normalized, self.compact, self.token, self.transliterated
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
    )


def best_names(sources: tuple[Name, ...], target: Name) -> NameEvidence:
    evidence = tuple(compare_names(source, target) for source in sources)
    return NameEvidence(
        exact=max((e.exact for e in evidence), default=Decimal(0)),
        normalized=max((e.normalized for e in evidence), default=Decimal(0)),
        compact=max((e.compact for e in evidence), default=Decimal(0)),
        token=max((e.token for e in evidence), default=Decimal(0)),
        transliterated=max((e.transliterated for e in evidence), default=Decimal(0)),
    )
