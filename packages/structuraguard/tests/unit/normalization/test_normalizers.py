"""Наблюдаемые contracts conservative scalar normalization."""

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest

from structuraguard.contracts.common import (
    BooleanScalar,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NullScalar,
    NumberScalar,
    RawScalar,
    StringScalar,
)
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizationResult,
    NormalizerSpec,
)
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.normalization import NormalizerRegistry


def normalize(
    value: RawScalar,
    *names: str,
    policy: NormalizationPolicy | None = None,
) -> NormalizationResult:
    return (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize(
            value,
            steps=tuple(NormalizerSpec(normalizer_id=name) for name in names),
            policy=policy or NormalizationPolicy(),
        )
    )


@pytest.mark.parametrize(
    ("raw", "name", "expected"),
    [
        ("  ООО Альфа  ", "trim", StringScalar(value="ООО Альфа")),
        ("", "empty_to_null", NullScalar()),
        ("ДА", "boolean", BooleanScalar(value=True)),
        ("нет", "boolean", BooleanScalar(value=False)),
        ("1250", "integer", IntegerScalar(value=1250)),
        ("02.09.2026", "date", DateScalar(value=date(2026, 9, 2))),
        (
            "02.09.2026 15:30:00+03:00",
            "datetime",
            DateTimeScalar(value=datetime(2026, 9, 2, 12, 30, tzinfo=UTC)),
        ),
        ("+7 (999) 123-45-67", "phone", StringScalar(value="+79991234567")),
        ("User+tag@EXAMPLE.COM", "email", StringScalar(value="User+tag@example.com")),
        (
            "550E8400E29B41D4A716446655440000",
            "uuid",
            StringScalar(value="550e8400-e29b-41d4-a716-446655440000"),
        ),
    ],
)
def test_builtins_preserve_raw_and_record_every_change(
    raw: str, name: str, expected: RawScalar
) -> None:
    source = StringScalar(value=raw)
    result = normalize(
        source, name, policy=NormalizationPolicy(locale=LocalePolicy.RU_RU)
    )
    assert result.accepted
    assert result.raw_value == source
    assert result.input_value == source
    assert result.normalized_value == expected
    assert result.steps[0].output.transformations
    current: RawScalar = source
    for change in result.steps[0].output.transformations:
        assert change.input_value == current
        current = change.output_value
    assert current == expected
    assert NormalizationResult.model_validate_json(result.model_dump_json()) == result


def test_money_trace_preserves_decimal_and_currency_operations() -> None:
    source = StringScalar(value=" 1\u202f250\u202f000,50 руб. ")
    with localcontext() as context:
        context.prec = 2
        result = normalize(
            source,
            "trim",
            "money",
            policy=NormalizationPolicy(locale=LocalePolicy.RU_RU, currency="RUB"),
        )
    assert result.accepted
    assert result.normalized_value == DecimalScalar(value=Decimal("1250000.50"))
    assert result.raw_value == source
    assert [
        t.operation for step in result.steps for t in step.output.transformations
    ] == [
        "trim",
        "remove_currency_symbol",
        "remove_group_separator",
        "replace_decimal_separator",
        "parse_decimal",
    ]


@pytest.mark.parametrize(
    ("raw", "name", "code"),
    [
        ("01/02/2026", "date", "AMBIGUOUS_DATE"),
        ("1,250", "decimal", "AMBIGUOUS_NUMBER"),
        ("2026-09-02T12:30:00", "datetime", "NORMALIZATION_TIMEZONE_REQUIRED"),
        ("31.02.2026", "date", "NORMALIZATION_INVALID_VALUE"),
        ("00123", "integer", "NORMALIZATION_LOSSY_CONVERSION"),
        ("1.5", "integer", "NORMALIZATION_INVALID_VALUE"),
        ("perhaps", "boolean", "NORMALIZATION_INVALID_VALUE"),
        ("79991234567", "phone", "NORMALIZATION_INVALID_VALUE"),
        ("+7 (999 123-45-67", "phone", "NORMALIZATION_INVALID_VALUE"),
        ("+79991234567 ext 1", "phone", "NORMALIZATION_INVALID_VALUE"),
        ("user..name@example.com", "email", "NORMALIZATION_INVALID_VALUE"),
        ("a@-example.com", "email", "NORMALIZATION_INVALID_VALUE"),
        ("not-a-uuid", "uuid", "NORMALIZATION_INVALID_VALUE"),
    ],
)
def test_invalid_and_ambiguous_values_are_not_repaired(
    raw: str, name: str, code: str
) -> None:
    source = StringScalar(value=raw)
    result = normalize(source, name)
    assert not result.accepted
    assert result.raw_value == result.normalized_value == source
    assert result.issues[0].code == code


def test_failed_chain_returns_original_input_and_keeps_attempted_steps() -> None:
    source = StringScalar(value=" 01/02/2026 ")
    result = normalize(source, "trim", "date", "uuid")
    assert result.normalized_value == source
    assert len(result.steps) == 2
    assert result.steps[0].output.value == StringScalar(value="01/02/2026")
    assert result.steps[1].output.issue_code == "AMBIGUOUS_DATE"
    assert not result.accepted


@pytest.mark.parametrize("name", ["integer", "decimal", "money"])
@pytest.mark.parametrize("source", [BooleanScalar(value=True), NumberScalar(value=1.0)])
def test_numeric_normalizers_do_not_coerce_bool_or_float(
    name: str, source: RawScalar
) -> None:
    result = normalize(source, name)
    assert not result.accepted
    assert result.normalized_value == source


@pytest.mark.parametrize(
    ("locale", "expected"),
    [(LocalePolicy.EN_US, date(2026, 1, 2)), (LocalePolicy.EN_GB, date(2026, 2, 1))],
)
def test_explicit_locale_resolves_date(locale: LocalePolicy, expected: date) -> None:
    result = normalize(
        StringScalar(value="01/02/2026"),
        "date",
        policy=NormalizationPolicy(locale=locale),
    )
    assert result.normalized_value == DateScalar(value=expected)


def test_empty_to_null_is_explicit_and_null_flows_through_nullable_chain() -> None:
    assert normalize(
        StringScalar(value=" "), "empty_to_null"
    ).normalized_value == StringScalar(value=" ")
    result = normalize(StringScalar(value=" \t"), "trim", "empty_to_null", "decimal")
    assert result.accepted
    assert result.normalized_value == NullScalar()
    assert len(result.steps) == 3


def test_currency_mismatch_and_mixed_grouping_are_rejected() -> None:
    policy = NormalizationPolicy(locale=LocalePolicy.RU_RU, currency="RUB")
    assert (
        normalize(StringScalar(value="10 USD"), "money", policy=policy).issues[0].code
        == "NORMALIZATION_CURRENCY_MISMATCH"
    )
    assert not normalize(
        StringScalar(value="1 234\u00a0567,00"), "decimal", policy=policy
    ).accepted


def test_boolean_tokens_are_configurable_without_casefold_collisions() -> None:
    policy = NormalizationPolicy(true_tokens=("on",), false_tokens=("off",))
    assert normalize(
        StringScalar(value="ON"), "boolean", policy=policy
    ).normalized_value == BooleanScalar(value=True)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NormalizationPolicy(true_tokens=("TRUE",), false_tokens=("true",))


@pytest.mark.parametrize("text", ["2026-09-02T12:30Z", "2026-09-02T15:30+03:00"])
def test_datetime_accepts_explicit_offset_with_minute_precision(text: str) -> None:
    result = normalize(StringScalar(value=text), "datetime")
    assert result.accepted
    assert result.normalized_value == DateTimeScalar(
        value=datetime(2026, 9, 2, 12, 30, tzinfo=UTC)
    )


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-02T12:00:60Z",
        "2026-09-02T12:00:00+00:99",
        "2026-09-02T12:00:00.1234567Z",
        "0001-01-01T00:00:00+01:00",
        "9999-12-31T23:59:59-01:00",
    ],
)
def test_datetime_never_repairs_precision_offsets_or_calendar_overflow(
    text: str,
) -> None:
    result = normalize(StringScalar(value=text), "datetime")
    assert not result.accepted
    assert result.normalized_value.value == text


def test_money_preserves_native_decimal_scale_and_signed_zero() -> None:
    source = DecimalScalar(value=Decimal("-0.0000"))
    result = normalize(source, "money")
    assert isinstance(result.raw_value, DecimalScalar)
    assert isinstance(result.normalized_value, DecimalScalar)
    assert result.raw_value.value.as_tuple() == source.value.as_tuple()
    assert result.normalized_value.value.as_tuple() == source.value.as_tuple()
    assert result.steps[0].output.transformations == ()
