"""Локали, закрытые patterns и консервативная типовая совместимость."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import (
    BooleanScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    StringScalar,
)
from structuraguard.contracts.profiling import LocalePolicy, NormalizedProfilingOptions
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("person@example.org", "email"),
        ("+7 (999) 123-45-67", "phone"),
        ("550e8400-e29b-41d4-a716-446655440000", "uuid"),
        ("https://example.org/path?q=1", "url"),
        ("01.09.2026", "date"),
        ("2026-09-01T14:00:00+03:00", "datetime"),
        ("7707083893", "russian_inn_10"),
        ("500100732259", "russian_inn_12"),
        ("125 000,50 RUB", "money"),
        ("true", "boolean"),
    ],
)
async def test_pattern_positive(text: str, pattern: str) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": StringScalar(value=text)}] * 25)
    )
    assert pattern in {p.code for p in result.fields[0].patterns}


@pytest.mark.parametrize(
    ("text", "pattern"),
    [
        ("a@@example.org", "email"),
        ("0000000000", "russian_inn_10"),
        ("7707083894", "russian_inn_10"),
        ("500100732250", "russian_inn_12"),
        ("2026-02-30", "date"),
        ("javascript:alert(1)", "url"),
        ("https://example.org:999999", "url"),
        ("12,34,567.80", "decimal"),
        ("uuid=550e8400-e29b-41d4-a716-446655440000", "uuid"),
    ],
)
async def test_pattern_negative(text: str, pattern: str) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": StringScalar(value=text)}])
    )
    assert pattern not in {p.code for p in result.fields[0].patterns}


@pytest.mark.parametrize(
    ("locale", "text", "month"),
    [
        (LocalePolicy.RU_RU, "01.09.2026", 9),
        (LocalePolicy.EN_US, "01/09/2026", 1),
        (LocalePolicy.EN_GB, "01/09/2026", 9),
    ],
)
async def test_date_locale_has_calendar_semantics(
    locale: LocalePolicy, text: str, month: int
) -> None:
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=locale)
    ).profile(normalized_stream([{"x": StringScalar(value=text)}] * 25))
    candidate = next(m for m in result.fields[0].candidate_extrema if m.kind == "date")
    assert candidate.minimum.kind == "date"
    assert candidate.minimum.value.month == month


@pytest.mark.parametrize(
    ("locale", "text"),
    [(LocalePolicy.EN_US, "125 000,50"), (LocalePolicy.RU_RU, "125,000.50")],
)
async def test_foreign_number_format_is_not_silently_reinterpreted(
    locale: LocalePolicy, text: str
) -> None:
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=locale)
    ).profile(normalized_stream([{"x": StringScalar(value=text)}] * 25))
    assert result.fields[0].inference.inferred_type == "string"
    assert result.fields[0].candidate_extrema == ()


async def test_decimal_context_and_money_representation_are_stable() -> None:
    values = [
        {"amount": DecimalScalar(value=Decimal("123456789.10"))},
        {"amount": DecimalScalar(value=Decimal("0.01"))},
    ]
    first = await NormalizedDataProfiler().profile(
        normalized_stream(values, semantic_type="money")
    )
    with localcontext() as ctx:
        ctx.prec = 2
        second = await NormalizedDataProfiler().profile(
            normalized_stream(values, semantic_type="money")
        )
    assert first == second
    assert first.fields[0].minimum == DecimalScalar(value=Decimal("0.01"))
    assert first.fields[0].inference.inferred_type == "money"


@pytest.mark.parametrize(
    "values",
    [
        (IntegerScalar(value=1), BooleanScalar(value=True)),
        (StringScalar(value="2026-01-01"), StringScalar(value="other")),
        (StringScalar(value="0"), StringScalar(value="1")),
    ],
)
async def test_incompatible_candidates_are_ranked_without_winner(
    values: tuple[NormalizedScalar, NormalizedScalar],
) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": value} for value in values] * 20)
    )
    assert result.fields[0].inference.status == "ambiguous"
    assert result.fields[0].inference.inferred_type is None
    assert len(result.fields[0].inference.candidates) >= 2


async def test_null_only_zero_denominators_and_literal_null_strings() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [
                {"empty": NullScalar(), "literal": StringScalar(value="null")},
                {"empty": NullScalar(), "literal": StringScalar(value="")},
            ]
        )
    )
    empty = result.field("row", "empty")
    assert (
        empty.null_ratio == 1
        and empty.unique_ratio is None
        and empty.mean_length is None
    )
    assert result.field("row", "literal").null_count == 0


async def test_datetime_is_utc_and_naive_string_is_ambiguous() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [
                {
                    "typed": DateTimeScalar(value=datetime(2026, 1, 1, tzinfo=UTC)),
                    "text": StringScalar(value="2026-01-01T12:30"),
                }
            ]
            * 25
        )
    )
    assert result.field("row", "typed").inference.inferred_type == "datetime"
    assert result.field("row", "text").inference.status == "ambiguous"


@pytest.mark.parametrize("text", ["1USD2", "12RUB34", "1.25 USD EUR"])
async def test_currency_must_be_a_single_prefix_or_suffix(text: str) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": StringScalar(value=text)}] * 25)
    )
    assert "money" not in {p.code for p in result.fields[0].patterns}


async def test_conflicting_currency_extrema_are_not_merged() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"x": StringScalar(value="100 USD")}, {"x": StringScalar(value="200 EUR")}]
        )
    )
    assert result.fields[0].inference.status == "ambiguous"
    assert result.fields[0].candidate_extrema == ()


async def test_declared_email_conflicting_with_values_is_ambiguous() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"x": IntegerScalar(value=i)} for i in range(25)], semantic_type="email"
        )
    )
    assert result.fields[0].inference.status == "ambiguous"
    assert "declared_type_conflict" in result.fields[0].inference.reasons


@pytest.mark.parametrize(
    "text", ["https://bad..host", "https://-bad.example", "https://host\\bad/"]
)
async def test_url_host_must_be_syntactically_valid(text: str) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": StringScalar(value=text)}])
    )
    assert "url" not in {p.code for p in result.fields[0].patterns}
