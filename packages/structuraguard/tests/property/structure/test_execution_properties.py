"""Независимые properties raw values, provenance, cardinality и rebatching."""

import asyncio
import json

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.structure.test_execution import execute, prepared

from structuraguard.contracts.source import TabularCellLocation
from structuraguard.parsers.builtin import DelimitedTextParser, JsonDocumentParser


@settings(max_examples=12, deadline=None)
@given(
    values=st.lists(st.integers(-999, 999), min_size=2, max_size=12),
    batch_size=st.integers(1, 6),
)
def test_raw_values_and_coordinates_survive_rebatching(
    values: list[int], batch_size: int
) -> None:
    content = (
        "key,value\n" + "".join(f"r{i},{value}\n" for i, value in enumerate(values))
    ).encode()

    async def check() -> None:
        expected = [
            text for i, value in enumerate(values) for text in (f"r{i}", str(value))
        ]
        for size in {1, batch_size}:
            request, batches = await prepared(
                DelimitedTextParser(), content, batch_size=size
            )
            output = await execute(request, batches)
            repeated = await execute(request, batches)
            assert output == repeated
            actual = [
                v
                for batch in output
                for r in batch.records
                for e in r.entities
                for v in e.values
            ]
            assert [v.raw_value.value for v in actual] == expected
            physical = {ref for b in batches for ref in b.physical_refs()}
            assert all(set(v.source_refs) <= physical for v in actual)
            assert len({v.value_id for v in actual}) == len(actual)
            for row, pair in enumerate(zip(actual[::2], actual[1::2], strict=True), 1):
                for v in pair:
                    location = v.origins[0].location
                    assert isinstance(location, TabularCellLocation)
                    assert location.row_index == row

    asyncio.run(check())


@settings(max_examples=12, deadline=None)
@given(children=st.lists(st.integers(0, 3), min_size=2, max_size=6))
def test_nested_child_count_equals_source_without_cartesian_product(
    children: list[int],
) -> None:
    data = [
        {"id": i, "items": [{"value": f"{i}:{j}"} for j in range(count)]}
        for i, count in enumerate(children)
    ]

    async def check() -> None:
        request, batches = await prepared(
            JsonDocumentParser(), json.dumps(data).encode(), batch_size=1
        )
        output = await execute(request, batches)
        records = [r for batch in output for r in batch.records]
        assert len(records) == len(children)
        for record, count in zip(records, children, strict=True):
            assert len(record.entities) == count + 1
            assert all(
                e.parent_entity_id == record.entities[0].entity_id
                for e in record.entities[1:]
            )

    asyncio.run(check())
