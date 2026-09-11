"""Генеративные oracle для ragged entities, typed extrema и границ distinct."""

import asyncio
from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from uuid import UUID

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.profiling import make_record, normalized_stream, record_stream

from structuraguard.contracts.common import (
    BooleanScalar,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    NumberScalar,
    StringScalar,
)
from structuraguard.contracts.profiling import ExamplePolicy, NormalizedProfilingOptions
from structuraguard.profiling import NormalizedDataProfiler


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.lists(
            st.tuples(
                st.sampled_from(["order", "item"]),
                st.dictionaries(
                    st.sampled_from(["x", "y"]),
                    st.one_of(st.none(), st.integers(-99, 99)),
                ),
            ),
            min_size=1,
            max_size=4,
        ),
        min_size=1,
        max_size=10,
    ),
    st.integers(1, 5),
)
def test_ragged_repeated_entities_match_independent_count_oracle(
    rows: list[list[tuple[str, dict[str, int | None]]]], batch_size: int
) -> None:
    async def run() -> None:
        records = [
            make_record(
                [
                    (
                        kind,
                        {
                            "anchor": IntegerScalar(value=0),
                            **{
                                key: IntegerScalar(value=value)
                                if value is not None
                                else NullScalar()
                                for key, value in row.items()
                            },
                        },
                    )
                    for kind, row in entities
                ],
                i,
            )
            for i, entities in enumerate(rows)
        ]
        result = await NormalizedDataProfiler().profile(
            record_stream(
                tuple(records[i : i + batch_size])
                for i in range(0, len(records), batch_size)
            )
        )
        counts = Counter(kind for entities in rows for kind, _ in entities)
        assert result.entity_count == sum(counts.values())
        for field in result.fields:
            if field.field.field_name == "anchor":
                continue
            kind, name = field.field.entity_type, field.field.field_name
            observed = [
                row[name]
                for entities in rows
                for entity_type, row in entities
                if kind == entity_type and name in row
            ]
            non_null = [v for v in observed if v is not None]
            nulls = counts[kind] - len(non_null)
            assert field.entity_count == counts[kind]
            assert field.present_count == len(observed)
            assert field.non_null_count == len(non_null)
            assert field.explicit_null_count == observed.count(None)
            assert field.missing_count == counts[kind] - len(observed)
            assert field.null_ratio == (Decimal(nulls) / counts[kind]).quantize(
                Decimal("0.000001")
            )
            assert field.unique_count == (len(set(non_null)) if non_null else None)

    asyncio.run(run())


@settings(max_examples=30, deadline=None)
@given(
    st.lists(
        st.one_of(
            st.integers(-100000, 100000).map(lambda v: IntegerScalar(value=v)),
            st.decimals(-1000, 1000, places=3).map(lambda v: DecimalScalar(value=v)),
            st.floats(-1000, 1000, allow_nan=False, allow_infinity=False).map(
                lambda v: NumberScalar(value=v)
            ),
            st.booleans().map(lambda v: BooleanScalar(value=v)),
            st.dates(date(2000, 1, 1), date(2030, 12, 31)).map(
                lambda v: DateScalar(value=v)
            ),
            st.datetimes(
                datetime(2000, 1, 1), datetime(2030, 12, 31), timezones=st.just(UTC)
            ).map(lambda v: DateTimeScalar(value=v)),
            st.text(alphabet=st.characters(codec="utf-8"), max_size=20).map(
                lambda v: StringScalar(value=v)
            ),
            st.just(NullScalar()),
        ),
        min_size=1,
        max_size=30,
    )
)
def test_all_scalar_extrema_use_compatible_families_and_ignore_decimal_context(
    values: list[NormalizedScalar],
) -> None:
    async def run() -> None:
        rows = [{"x": value} for value in values]
        profiler = NormalizedDataProfiler()
        result = await profiler.profile(normalized_stream(rows))
        with localcontext() as context:
            context.prec = 2
            changed = await profiler.profile(normalized_stream(rows))
        assert result == changed
        field = result.fields[0]
        expected: dict[str, tuple[object, object, int]] = {}
        numeric = [
            v.value for v in values if isinstance(v, IntegerScalar | DecimalScalar)
        ]
        numbers = [v.value for v in values if isinstance(v, NumberScalar)]
        booleans = [v.value for v in values if isinstance(v, BooleanScalar)]
        dates = [v.value for v in values if isinstance(v, DateScalar)]
        instants = [v.value for v in values if isinstance(v, DateTimeScalar)]
        strings = [v.value for v in values if isinstance(v, StringScalar)]
        if numeric:
            expected["numeric"] = min(numeric), max(numeric), len(numeric)
        if numbers:
            expected["number"] = min(numbers), max(numbers), len(numbers)
        if booleans:
            expected["boolean"] = min(booleans), max(booleans), len(booleans)
        if dates:
            expected["date"] = min(dates), max(dates), len(dates)
        if instants:
            expected["datetime"] = min(instants), max(instants), len(instants)
        if strings:
            expected["string"] = min(strings), max(strings), len(strings)
        assert {metric.kind for metric in field.extrema} == set(expected)
        for metric in field.extrema:
            assert (
                metric.minimum.value,
                metric.maximum.value,
                metric.count,
            ) == expected[metric.kind]
        assert field.total_length == sum(map(len, strings))
        if strings:
            assert field.min_length == min(map(len, strings))
            assert field.max_length == max(map(len, strings))
            assert field.mean_length == (
                Decimal(sum(map(len, strings))) / len(strings)
            ).quantize(Decimal("0.000001"))

    asyncio.run(run())


@settings(max_examples=15, deadline=None)
@given(st.sampled_from([15, 16, 17]), st.integers(1, 8))
def test_distinct_k_boundary_and_duplicates_match_exact_oracle(
    count: int, repeats: int
) -> None:
    async def run() -> None:
        result = await NormalizedDataProfiler(
            NormalizedProfilingOptions(distinct_k=16, examples=ExamplePolicy.OMIT)
        ).profile(
            normalized_stream(
                {"id": IntegerScalar(value=i)}
                for _ in range(repeats)
                for i in range(count)
            )
        )
        field = result.fields[0]
        assert field.unique_mode == ("exact" if count <= 16 else "estimated")
        assert field.distinct_algorithm == "kmv128_v1" and field.distinct_k == 16
        if count <= 16:
            assert field.unique_count == count
            assert field.unique_ratio == (Decimal(1) / repeats).quantize(
                Decimal("0.000001")
            )
        assert field.non_null_count == count * repeats
        assert result.sample_bytes == 0 and not field.examples

    asyncio.run(run())


@settings(max_examples=20, deadline=None)
@given(st.dates(), st.uuids())
def test_generated_dates_and_uuids_are_recognized_without_coercion(
    day: date, identifier: UUID
) -> None:
    async def run() -> None:
        rows = [
            {
                "day": StringScalar(value=day.isoformat()),
                "key": StringScalar(value=str(identifier)),
            }
        ]
        result = await NormalizedDataProfiler().profile(normalized_stream(rows))
        assert "date" in {p.code for p in result.field("row", "day").patterns}
        assert "uuid" in {p.code for p in result.field("row", "key").patterns}
        assert result.field("row", "day").candidate_extrema[0].minimum.value == day
        assert result.field("row", "key").minimum == rows[0]["key"]

    asyncio.run(run())


@settings(max_examples=20, deadline=None)
@given(st.integers(1000000000, 9999999999), st.booleans())
def test_generated_inn_check_digits_reject_one_digit_corruption(
    stem: int, personal: bool
) -> None:
    digits = [int(c) for c in str(stem)]
    weights: tuple[int, ...]
    if personal:
        digits.append(
            sum(
                v * w
                for v, w in zip(digits, (7, 2, 4, 10, 3, 5, 9, 4, 6, 8), strict=True)
            )
            % 11
            % 10
        )
        weights = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
    else:
        digits = digits[:9]
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    digits.append(sum(v * w for v, w in zip(digits, weights, strict=True)) % 11 % 10)
    valid = "".join(map(str, digits))
    invalid = valid[:-1] + str((digits[-1] + 1) % 10)

    async def run() -> None:
        result = await NormalizedDataProfiler().profile(
            normalized_stream(
                [
                    {
                        "valid": StringScalar(value=valid),
                        "invalid": StringScalar(value=invalid),
                    }
                ]
            )
        )
        pattern = "russian_inn_12" if personal else "russian_inn_10"
        assert pattern in {p.code for p in result.field("row", "valid").patterns}
        assert pattern not in {p.code for p in result.field("row", "invalid").patterns}

    asyncio.run(run())
