"""Normalization properties: Unicode, locales и lossless round-trip."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from structuraguard.contracts.common import DateScalar, DecimalScalar, StringScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizationResult,
    NormalizerSpec,
)
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.normalization import NormalizerRegistry


@given(st.text(alphabet=st.characters(codec="utf-8"), max_size=1000))
def test_unicode_trim_is_idempotent_and_keeps_original(raw: str) -> None:
    registry = NormalizerRegistry.with_builtins().snapshot()
    steps = (NormalizerSpec(normalizer_id="trim"),)
    result = registry.normalize(StringScalar(value=raw), steps=steps)
    assert result.accepted
    assert result.raw_value.value == raw
    assert result.normalized_value == StringScalar(value=raw.strip())
    again = registry.normalize(result.normalized_value, steps=steps)
    assert again.normalized_value == result.normalized_value
    assert again.steps[0].output.transformations == ()
    assert NormalizationResult.model_validate_json(result.model_dump_json()) == result


@given(
    st.integers(min_value=0, max_value=999_999_999),
    st.integers(min_value=0, max_value=99),
    st.sampled_from((" ", "\u00a0", "\u202f")),
)
def test_ru_group_separators_preserve_exact_decimal(
    whole: int, cents: int, separator: str
) -> None:
    raw = f"{whole:,}".replace(",", separator) + f",{cents:02}"
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(
            StringScalar(value=raw),
            steps=(NormalizerSpec(normalizer_id="decimal"),),
            policy=NormalizationPolicy(locale=LocalePolicy.RU_RU),
        )
    )
    assert result.accepted
    assert result.raw_value.value == raw
    assert result.normalized_value == DecimalScalar(
        value=Decimal(f"{whole}.{cents:02}")
    )
    assert NormalizationResult.model_validate_json(result.model_dump_json()) == result


@given(
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=1, max_value=12),
    st.integers(min_value=1900, max_value=2100),
)
def test_unknown_slash_locale_never_selects_between_two_dates(
    first: int, second: int, year: int
) -> None:
    raw = StringScalar(value=f"{first:02}/{second:02}/{year:04}")
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(raw, steps=(NormalizerSpec(normalizer_id="date"),))
    )
    if first != second:
        assert not result.accepted
        assert result.issues[0].code == "AMBIGUOUS_DATE"
        assert result.normalized_value == raw
    else:
        assert result.normalized_value == DateScalar(value=date(year, first, second))


@given(st.dates(min_value=date(1, 1, 1), max_value=date(9999, 12, 31)))
def test_iso_calendar_dates_round_trip(day: date) -> None:
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(
            StringScalar(value=day.isoformat()),
            steps=(NormalizerSpec(normalizer_id="date"),),
        )
    )
    assert result.accepted
    assert result.normalized_value == DateScalar(value=day)
    assert NormalizationResult.model_validate_json(result.model_dump_json()) == result


@given(
    st.sampled_from(("１", "١", "𝟙", "²")),
    st.sampled_from(("integer", "decimal", "phone")),
)
def test_unicode_digits_are_not_silently_converted(digit: str, normalizer: str) -> None:
    raw = StringScalar(value=("+" + digit * 10) if normalizer == "phone" else digit)
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(raw, steps=(NormalizerSpec(normalizer_id=normalizer),))
    )
    assert not result.accepted
    assert result.normalized_value == raw


@given(st.uuids())
def test_uuid_canonicalization_round_trips_without_changing_identity(
    value: UUID,
) -> None:
    raw = StringScalar(value=value.hex.upper())
    registry = NormalizerRegistry.with_builtins().snapshot()
    steps = (NormalizerSpec(normalizer_id="uuid"),)
    result = registry.normalize(raw, steps=steps)
    assert result.accepted
    assert result.raw_value == raw
    assert result.normalized_value == StringScalar(value=str(value))
    assert (
        registry.normalize(result.normalized_value, steps=steps)
        .steps[0]
        .output.transformations
        == ()
    )


@given(
    st.integers(min_value=1, max_value=999), st.integers(min_value=100, max_value=999)
)
def test_unspecified_numeric_separator_never_guesses_grouping(
    whole: int, tail: int
) -> None:
    raw = StringScalar(value=f"{whole},{tail:03}")
    registry = NormalizerRegistry.with_builtins().snapshot()
    steps = (NormalizerSpec(normalizer_id="decimal"),)
    ambiguous = registry.normalize(raw, steps=steps)
    assert ambiguous.issues[0].code == "AMBIGUOUS_NUMBER"
    assert ambiguous.normalized_value == raw
    ru = registry.normalize(
        raw, steps=steps, policy=NormalizationPolicy(locale=LocalePolicy.RU_RU)
    )
    en = registry.normalize(
        raw, steps=steps, policy=NormalizationPolicy(locale=LocalePolicy.EN_US)
    )
    assert ru.normalized_value == DecimalScalar(value=Decimal(f"{whole}.{tail:03}"))
    assert en.normalized_value == DecimalScalar(value=Decimal(whole * 1000 + tail))


@given(
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        min_size=1,
        max_size=30,
    )
)
def test_email_canonicalization_preserves_case_sensitive_local_part(local: str) -> None:
    registry = NormalizerRegistry.with_builtins().snapshot()
    steps = (NormalizerSpec(normalizer_id="email"),)
    result = registry.normalize(
        StringScalar(value=local + "+tag@EXAMPLE.COM"), steps=steps
    )
    assert result.normalized_value == StringScalar(value=local + "+tag@example.com")
    assert (
        registry.normalize(result.normalized_value, steps=steps)
        .steps[0]
        .output.transformations
        == ()
    )


@given(
    st.text(alphabet="0123456789", min_size=6, max_size=14),
    st.sampled_from((" ", "-", "\u00a0", "\u202f")),
)
def test_phone_separators_never_change_digits(tail: str, separator: str) -> None:
    digits = "7" + tail
    raw = StringScalar(value="+" + separator.join(digits))
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(raw, steps=(NormalizerSpec(normalizer_id="phone"),))
    )
    assert result.accepted
    assert result.raw_value == raw
    assert result.normalized_value == StringScalar(value="+" + digits)
