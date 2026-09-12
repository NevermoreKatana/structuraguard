"""Conservative built-ins: закрытые grammars, точные числа и явная история."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from structuraguard.contracts.common import (
    BooleanScalar,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NullScalar,
    RawScalar,
    StringScalar,
)
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizerDescriptor,
    NormalizerOutput,
    NormalizerParameter,
    ValueTransformation,
)
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.domain.scalar_grammar import (
    date_candidates,
    decimal_text_candidates,
)
from structuraguard.ports.normalization import Normalizer

_GROUP_SPACES = " \u00a0\u202f"
_CURRENCIES = (
    ("руб.", "RUB"),
    ("руб", "RUB"),
    ("RUB", "RUB"),
    ("USD", "USD"),
    ("EUR", "EUR"),
    ("GBP", "GBP"),
    ("₽", "RUB"),
    ("$", "USD"),
    ("€", "EUR"),
    ("£", "GBP"),
)
_BUILTINS = (
    "trim",
    "empty_to_null",
    "boolean",
    "integer",
    "decimal",
    "money",
    "date",
    "datetime",
    "phone",
    "email",
    "uuid",
)
_INVALID = "NORMALIZATION_INVALID_VALUE"


class _Changes:
    def __init__(self, value: RawScalar) -> None:
        self.original = value
        self.value = value
        self.changes: list[ValueTransformation] = []

    def set(self, operation: str, value: RawScalar) -> None:
        if value.model_dump_json() != self.value.model_dump_json():
            self.changes.append(
                ValueTransformation(
                    operation=operation, input_value=self.value, output_value=value
                )
            )
            self.value = value

    def done(self) -> NormalizerOutput:
        return NormalizerOutput(value=self.value, transformations=tuple(self.changes))

    def reject(self, code: str = _INVALID) -> NormalizerOutput:
        return NormalizerOutput(value=self.original, issue_code=code)


def _strip_currency(
    changes: _Changes, text: str, policy: NormalizationPolicy
) -> tuple[str, str | None]:
    for token, currency in _CURRENCIES:
        folded = text.casefold()
        if not (
            folded.startswith(token.casefold()) or folded.endswith(token.casefold())
        ):
            continue
        if policy.currency is None:
            return text, "NORMALIZATION_CURRENCY_REQUIRED"
        if currency != policy.currency:
            return text, "NORMALIZATION_CURRENCY_MISMATCH"
        if folded.startswith(token.casefold()):
            cleaned = text[len(token) :].lstrip(_GROUP_SPACES)
        else:
            cleaned = text[: -len(token)].rstrip(_GROUP_SPACES)
        changes.set("remove_currency_symbol", StringScalar(value=cleaned))
        return cleaned, None
    return text, None


def _numeric(
    changes: _Changes, kind: str, policy: NormalizationPolicy
) -> NormalizerOutput:
    value = changes.value
    if isinstance(value, IntegerScalar):
        if len(str(abs(value.value))) > policy.limits.max_numeric_digits:
            return changes.reject("SECURITY_LIMIT_EXCEEDED")
        if kind != "integer":
            changes.set("parse_decimal", DecimalScalar(value=Decimal(value.value)))
        return changes.done()
    if isinstance(value, DecimalScalar):
        if kind == "integer":
            # Fractional Decimal и любая float coercion требуют отдельной policy.
            if value.value.as_tuple().exponent != 0:
                return changes.reject(_INVALID)
            changes.set("parse_integer", IntegerScalar(value=int(value.value)))
        return changes.done()
    if not isinstance(value, StringScalar):
        return changes.reject()
    text = value.value
    if kind == "money":
        text, code = _strip_currency(changes, text, policy)
        if code is not None:
            return changes.reject(code)
    if sum("0" <= char <= "9" for char in text) > policy.limits.max_numeric_digits:
        return changes.reject("SECURITY_LIMIT_EXCEEDED")
    candidates = decimal_text_candidates(text, policy.locale)
    if not candidates:
        return changes.reject()
    if len({Decimal(candidate) for candidate in candidates}) > 1:
        return changes.reject("AMBIGUOUS_NUMBER")
    canonical = candidates[0]
    if len(canonical.partition(".")[2]) > policy.limits.max_decimal_exponent:
        return changes.reject("SECURITY_LIMIT_EXCEEDED")
    if len({char for char in text if char in _GROUP_SPACES}) > 1:
        return changes.reject()
    unsigned = canonical.lstrip("+-")
    if kind == "integer":
        if "." in canonical:
            return changes.reject()
        if len(unsigned) > 1 and unsigned.startswith("0"):
            return changes.reject("NORMALIZATION_LOSSY_CONVERSION")
    ru = policy.locale == LocalePolicy.RU_RU or (
        policy.locale == LocalePolicy.UNSPECIFIED
        and canonical
        == text.replace(" ", "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(",", ".")
    )
    cleaned = text
    for separator in _GROUP_SPACES if ru else ",":
        cleaned = cleaned.replace(separator, "")
    changes.set("remove_group_separator", StringScalar(value=cleaned))
    if ru:
        cleaned = cleaned.replace(",", ".")
        changes.set("replace_decimal_separator", StringScalar(value=cleaned))
    if kind == "integer":
        changes.set("parse_integer", IntegerScalar(value=int(cleaned)))
    else:
        changes.set("parse_decimal", DecimalScalar(value=Decimal(cleaned)))
    return changes.done()


def _boolean(changes: _Changes, policy: NormalizationPolicy) -> NormalizerOutput:
    if isinstance(changes.value, BooleanScalar):
        return changes.done()
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    token = changes.value.value.casefold()
    if token in tuple(item.casefold() for item in policy.true_tokens):
        parsed = True
    elif token in tuple(item.casefold() for item in policy.false_tokens):
        parsed = False
    else:
        return changes.reject()
    changes.set("casefold_boolean", StringScalar(value=token))
    changes.set("parse_boolean", BooleanScalar(value=parsed))
    return changes.done()


def _date(changes: _Changes, policy: NormalizationPolicy) -> NormalizerOutput:
    if isinstance(changes.value, DateScalar):
        return changes.done()
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    candidates = date_candidates(changes.value.value, policy.locale)
    if len(candidates) > 1:
        return changes.reject("AMBIGUOUS_DATE")
    if not candidates:
        return changes.reject()
    changes.set("parse_date", DateScalar(value=next(iter(candidates))))
    return changes.done()


def _datetime(changes: _Changes, policy: NormalizationPolicy) -> NormalizerOutput:
    if isinstance(changes.value, DateTimeScalar):
        return changes.done()
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    match = re.fullmatch(
        r"([0-9./-]{10})[T ]([0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?)(Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?",
        changes.value.value,
    )
    if match is None:
        return changes.reject()
    days = date_candidates(match[1], policy.locale)
    if len(days) > 1:
        return changes.reject("AMBIGUOUS_DATE")
    if not days:
        return changes.reject()
    if match[3] is None or match[3] == "-00:00":
        return changes.reject("NORMALIZATION_TIMEZONE_REQUIRED")
    day = next(iter(days))
    iso = day.isoformat() + "T" + match[2] + match[3]
    try:
        parsed = datetime.fromisoformat(iso).astimezone(UTC)
    except (ValueError, OverflowError):
        return changes.reject()
    changes.set("canonicalize_datetime", StringScalar(value=iso))
    canonical = parsed.isoformat().replace("+00:00", "Z")
    changes.set("normalize_utc_offset", StringScalar(value=canonical))
    changes.set("parse_datetime", DateTimeScalar(value=parsed))
    return changes.done()


def _phone(changes: _Changes) -> NormalizerOutput:
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    text = changes.value.value
    if (
        len(text) > 64
        or not text.startswith("+")
        or any(char not in "0123456789 ()-\u00a0\u202f" for char in text[1:])
    ):
        return changes.reject()
    depth = 0
    group_digits = 0
    for char in text[1:]:
        if char == "(":
            if depth:
                return changes.reject()
            depth = 1
            group_digits = 0
        elif char == ")":
            if not depth or not group_digits:
                return changes.reject()
            depth = 0
        elif "0" <= char <= "9" and depth:
            group_digits += 1
    if depth:
        return changes.reject()
    digits = "".join(char for char in text[1:] if "0" <= char <= "9")
    if not 7 <= len(digits) <= 15 or digits.startswith("0"):
        return changes.reject()
    changes.set("remove_phone_separators", StringScalar(value="+" + digits))
    return changes.done()


def _email(changes: _Changes) -> NormalizerOutput:
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    text = changes.value.value
    if not text.isascii() or len(text) > 254 or text.count("@") != 1:
        return changes.reject()
    local, domain = text.split("@")
    if not 1 <= len(local) <= 64 or any(not part for part in local.split(".")):
        return changes.reject()
    if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local) is None:
        return changes.reject()
    labels = domain.split(".")
    if len(labels) < 2 or not labels[-1].isalpha() or len(labels[-1]) < 2:
        return changes.reject()
    if any(
        not 1 <= len(label) <= 63
        or re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
        for label in labels
    ):
        return changes.reject()
    changes.set(
        "lowercase_email_domain", StringScalar(value=local + "@" + domain.lower())
    )
    return changes.done()


def _uuid(changes: _Changes) -> NormalizerOutput:
    if not isinstance(changes.value, StringScalar):
        return changes.reject()
    text = changes.value.value
    if (
        re.fullmatch(
            r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})",
            text,
        )
        is None
    ):
        return changes.reject()
    changes.set("canonicalize_uuid", StringScalar(value=str(UUID(text))))
    return changes.done()


@dataclass(frozen=True, slots=True)
class BuiltinNormalizer:
    """Одна стандартная pure implementation закрытого operator ID.

    Обычно создаётся через NormalizerRegistry.with_builtins(). Direct callers
    передают предварительно проверенные bounded DTO согласно Normalizer port.
    """

    normalizer_id: str

    def normalize(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput:
        """Выполнить один standard normalizer; все фактические изменения в trace.

        Неподдержанные parameters и значения возвращают issue_code без repair.
        Null проходит как отсутствие nullable значения; required проверяется
        следующим validation engine. I/O и semantic type inference отсутствуют.
        """
        changes = _Changes(value)
        if parameters:
            return changes.reject("NORMALIZATION_PARAMETERS_INVALID")
        if self.normalizer_id not in _BUILTINS:
            return changes.reject("NORMALIZER_NOT_FOUND")
        if isinstance(value, NullScalar):
            return changes.done()
        if self.normalizer_id == "trim":
            if isinstance(value, StringScalar):
                changes.set("trim", StringScalar(value=value.value.strip()))
            return changes.done()
        if self.normalizer_id == "empty_to_null":
            if isinstance(value, StringScalar) and value.value in policy.empty_tokens:
                changes.set("empty_to_null", NullScalar())
            return changes.done()
        if self.normalizer_id in ("integer", "decimal", "money"):
            return _numeric(changes, self.normalizer_id, policy)
        if self.normalizer_id == "boolean":
            return _boolean(changes, policy)
        if self.normalizer_id == "date":
            return _date(changes, policy)
        if self.normalizer_id == "datetime":
            return _datetime(changes, policy)
        if self.normalizer_id == "phone":
            return _phone(changes)
        if self.normalizer_id == "email":
            return _email(changes)
        return _uuid(changes)


def builtin_normalizers() -> tuple[tuple[NormalizerDescriptor, Normalizer], ...]:
    """Создать независимый immutable набор built-ins без глобальной регистрации."""
    return tuple(
        (NormalizerDescriptor(normalizer_id=name), BuiltinNormalizer(name))
        for name in _BUILTINS
    )
