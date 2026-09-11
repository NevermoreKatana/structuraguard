"""Независимые oracles для counts, samples, locales и canonical content."""

import asyncio
from decimal import Decimal, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import (
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    StringScalar,
)
from structuraguard.contracts.profiling import ExamplePolicy, NormalizedProfilingOptions
from structuraguard.profiling import NormalizedDataProfiler


@settings(max_examples=35, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.none(),
            st.integers(-10000, 10000),
            st.text(alphabet=st.characters(codec="utf-8"), max_size=40),
        ),
        max_size=50,
    ),
    st.integers(1, 12),
)
def test_counts_extrema_and_rebatching_match_offline_oracle(
    values: list[int | str | None], batch_size: int
) -> None:
    rows: list[dict[str, NormalizedScalar]] = []
    for value in values:
        scalar = (
            NullScalar()
            if value is None
            else IntegerScalar(value=value)
            if isinstance(value, int)
            else StringScalar(value=value)
        )
        rows.append({"x": scalar})

    async def run() -> None:
        profiler = NormalizedDataProfiler()
        first = await profiler.profile(normalized_stream(rows, batch_size=batch_size))
        second = await profiler.profile(
            normalized_stream(rows, batch_size=1, prefix="new")
        )
        assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
        if not rows:
            assert first.fields == ()
            return
        field = first.fields[0]
        assert field.present_count == len(values)
        assert field.explicit_null_count == values.count(None)
        unique = len({(type(value), value) for value in values if value is not None})
        assert field.unique_count == (unique if unique else None)
        strings = [value for value in values if isinstance(value, str)]
        assert field.total_length == sum(map(len, strings))
        assert field.min_length == (min(map(len, strings)) if strings else None)
        assert field.max_length == (max(map(len, strings)) if strings else None)
        assert field.examples == second.fields[0].examples
        assert first.sample_bytes <= profiler.options.max_sample_bytes

    asyncio.run(run())


@settings(max_examples=25, deadline=None)
@given(st.lists(st.integers(-100, 100), min_size=1, max_size=50), st.integers(0, 8))
def test_sampling_budget_and_global_decimal_context_do_not_change_statistics(
    values: list[int], count: int
) -> None:
    async def run() -> None:
        rows = [{"x": IntegerScalar(value=value)} for value in values]
        normal = await NormalizedDataProfiler().profile(normalized_stream(rows))
        with localcontext() as ctx:
            ctx.prec = 2
            small = await NormalizedDataProfiler(
                NormalizedProfilingOptions(examples_per_field=count)
            ).profile(normalized_stream(rows))
        assert normal.normalized_data_fingerprint == small.normalized_data_fingerprint
        assert normal.fields[0].unique_ratio == small.fields[0].unique_ratio
        assert normal.fields[0].inference == small.fields[0].inference
        assert len(small.fields[0].examples) <= count

    asyncio.run(run())


@settings(max_examples=25, deadline=None)
@given(st.integers(0, 100000), st.integers(0, 99))
def test_number_locale_policy_preserves_decimal_value(whole: int, cents: int) -> None:
    from structuraguard.contracts.profiling import LocalePolicy

    async def run() -> None:
        ru = f"{whole:,}".replace(",", " ") + f",{cents:02}"
        en = f"{whole:,}.{cents:02}"
        expected = Decimal(f"{whole}.{cents:02}")
        for policy, text in ((LocalePolicy.RU_RU, ru), (LocalePolicy.EN_US, en)):
            result = await NormalizedDataProfiler(
                NormalizedProfilingOptions(locale=policy)
            ).profile(normalized_stream([{"amount": StringScalar(value=text)}]))
            assert result.fields[0].candidate_extrema[0].minimum.value == expected

    asyncio.run(run())


@settings(max_examples=20, deadline=None)
@given(st.integers(17, 250))
def test_kmv_mode_is_explicit_and_omit_retains_no_samples(count: int) -> None:
    async def run() -> None:
        result = await NormalizedDataProfiler(
            NormalizedProfilingOptions(distinct_k=16, examples=ExamplePolicy.OMIT)
        ).profile(
            normalized_stream({"id": IntegerScalar(value=i)} for i in range(count))
        )
        field = result.fields[0]
        assert field.unique_mode == "estimated"
        assert field.unique_count is not None and 17 <= field.unique_count <= count
        assert field.examples == () and result.sample_bytes == 0

    asyncio.run(run())
