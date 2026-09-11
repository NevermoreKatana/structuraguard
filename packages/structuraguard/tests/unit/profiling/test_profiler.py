"""Наблюдаемый контракт profiler после semantic parsing."""

from decimal import Decimal

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar, NullScalar, StringScalar
from structuraguard.contracts.profiling import LocalePolicy, NormalizedProfilingOptions
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


async def test_empty_completed_dataset() -> None:
    result = await NormalizedDataProfiler().profile(normalized_stream([]))
    assert result.fields == () and result.record_count == 0
    assert result.normalized_data_fingerprint.startswith("sha256:")


async def test_sparse_mixed_unicode_and_null_denominators() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [
                {"x": StringScalar(value="ёж🙂")},
                {"x": NullScalar(), "late": IntegerScalar(value=7)},
                {"x": IntegerScalar(value=1)},
                {"other": StringScalar(value="")},
            ],
            batch_size=1,
        )
    )
    field = result.field("row", "x")
    assert (field.present_count, field.missing_count, field.null_count) == (3, 1, 2)
    assert field.null_ratio == Decimal("0.5")
    assert field.unique_count == 2 and field.unique_ratio == 1
    assert field.mean_length == 3
    assert field.inference.status == "ambiguous"
    assert field.minimum is None
    assert result.field("row", "late").null_ratio == Decimal("0.75")


@pytest.mark.parametrize(
    ("locale", "text", "expected"),
    [
        (LocalePolicy.RU_RU, "125 000,50", Decimal("125000.50")),
        (LocalePolicy.EN_US, "125,000.50", Decimal("125000.50")),
        (LocalePolicy.EN_GB, "125,000.50", Decimal("125000.50")),
    ],
)
async def test_locale_money_is_decimal(
    locale: LocalePolicy, text: str, expected: Decimal
) -> None:
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=locale)
    ).profile(normalized_stream([{"amount": StringScalar(value=text)}] * 25))
    field = result.field("row", "amount")
    numeric = next(
        metric for metric in field.candidate_extrema if metric.kind == "decimal"
    )
    assert numeric.minimum.value == expected
    assert isinstance(numeric.minimum.value, Decimal)


async def test_ambiguous_locale_does_not_choose_a_number_or_date() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"n": StringScalar(value="1,234"), "d": StringScalar(value="01/02/2026")}]
            * 25
        )
    )
    for name in ("n", "d"):
        assert result.field("row", name).inference.status == "ambiguous"


async def test_sampling_and_content_hash_are_independent_of_batching_and_ids() -> None:
    rows = [{"x": StringScalar(value=str(i))} for i in range(50)]
    profiler = NormalizedDataProfiler()
    first = await profiler.profile(normalized_stream(rows, batch_size=1))
    second = await profiler.profile(normalized_stream(rows, batch_size=9, prefix="new"))
    assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
    assert (
        first.normalized_manifest_fingerprint != second.normalized_manifest_fingerprint
    )
    assert first.fields[0].examples == second.fields[0].examples
    assert len(first.fields[0].examples) <= 8


async def test_safe_summary_drops_values_names_extrema_and_fingerprints() -> None:
    canary = "secret.person@example.com"
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{canary: StringScalar(value=canary)}])
    )
    assert canary not in repr(result)
    assert canary not in result.safe_summary().canonical_json()
    assert all(example.value is None for example in result.fields[0].examples)
    assert result.fields[0].pii.state == "detected"


async def test_distinct_overflow_is_explicit_and_samples_stay_bounded() -> None:
    options = NormalizedProfilingOptions(distinct_k=16)
    result = await NormalizedDataProfiler(options).profile(
        normalized_stream({"id": IntegerScalar(value=i)} for i in range(2000))
    )
    field = result.fields[0]
    assert field.unique_mode == "estimated"
    assert field.unique_count is not None
    assert 17 <= field.unique_count <= 2000
    assert field.identity.strength == "possible_identifier"
    assert field.sample_eligible == 2000 and len(field.examples) == 8


def test_kmv_fixed_ensemble_matches_independent_distinct_oracle() -> None:
    from structuraguard.profiling._bounded import Distinct
    from structuraguard.profiling._stream import Ledger

    for seed in range(5):
        sketch = Distinct(1024, Ledger(1024 * 1024))
        for _ in range(2):
            for value in range(5000):
                sketch.add(f"{seed}:{value}".encode())
        estimate = sketch.count(10000)
        assert estimate is not None and abs(estimate - 5000) / 5000 < 0.15
        assert len(sketch.values) == 1024
