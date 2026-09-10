"""Независимые properties shape/raw hints при изменении parser batches."""

import asyncio
from decimal import Decimal

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.unit.structure.test_profiling import profile_content

from structuraguard.contracts import StructureProfile
from structuraguard.contracts._base import canonical_json_value
from structuraguard.contracts.structure import (
    StructuralProfilingOptions,
    TabularObservation,
)
from structuraguard.parsers.builtin import DelimitedTextParser, PlainTextParser
from structuraguard.structure._samples import primitive


@settings(max_examples=100)
@example("-+1.5")
@given(st.text(alphabet="+-0123456789.", min_size=1, max_size=32))
def test_numeric_hint_is_always_a_valid_finite_decimal(value: str) -> None:
    if primitive(value) in {"integer", "decimal"}:
        assert Decimal(value).is_finite()


def projection(profile: StructureProfile) -> list[str]:
    return sorted(
        canonical_json_value(
            item.observation, exclude_top_level=frozenset({"source_refs"})
        )
        for item in profile.observations
    )


@settings(max_examples=15, deadline=None)
@given(
    rows=st.lists(
        st.tuples(st.integers(1, 999), st.integers(-999, 999)), min_size=1, max_size=15
    ),
    batch_size=st.integers(1, 8),
)
def test_tabular_projection_does_not_depend_on_batch_boundaries(
    rows: list[tuple[int, int]], batch_size: int
) -> None:
    content = (
        "id,amount\n" + "".join(f"{key},{value}\n" for key, value in rows)
    ).encode()

    async def compare() -> None:
        first = await profile_content(DelimitedTextParser(), content, batch_size=1)
        second = await profile_content(
            DelimitedTextParser(), content, batch_size=batch_size
        )
        assert projection(first) == projection(second)
        shape = next(
            item.observation
            for item in first.observations
            if isinstance(item.observation, TabularObservation)
            and item.observation.role == "shape"
        )
        assert shape.row_count == len(rows) + 1
        assert shape.column_count == 2
        assert shape.ragged_rows == 0

    asyncio.run(compare())


@settings(max_examples=15, deadline=None)
@given(
    count=st.integers(1, 100),
    budget=st.integers(1, 20),
    word=st.text(
        alphabet=st.characters(blacklist_categories=("Cc", "Cs")),
        min_size=1,
        max_size=20,
    ),
)
def test_unicode_sampling_counts_and_bounds(count: int, budget: int, word: str) -> None:
    content = ((word + "\n") * count).encode()

    async def check() -> None:
        profile = await profile_content(
            PlainTextParser(),
            content,
            options=StructuralProfilingOptions(max_sample_items=budget),
        )
        coverage = profile.coverage
        assert coverage is not None
        assert coverage.seen_items == count
        assert coverage.sampled_items <= budget
        assert coverage.sampled_items + coverage.skipped_items == count
        assert coverage.sampled_bytes <= 65_536
        assert all(item.source_refs for item in profile.observations)

    asyncio.run(check())
