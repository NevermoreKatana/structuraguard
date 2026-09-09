"""Ни chunks HTTP, ни batch boundaries не меняют XHTML tree/provenance."""

from __future__ import annotations

import asyncio
from html import escape

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.tika import RTF, FakeTikaServer, adapter_for
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for


@settings(max_examples=20, deadline=None)
@given(
    st.lists(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cc", "Cs"),
                blacklist_characters=("\ufffe", "\uffff"),
            ),
            max_size=20,
        ),
        min_size=1,
        max_size=5,
    ),
    st.integers(1, 31),
    st.integers(1, 4),
)
def test_tika_unicode_chunk_and_batch_invariance(
    values: list[str], chunk_size: int, batch_size: int
) -> None:
    content = (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        + "".join(f"<p>{escape(v)}</p>" for v in values)
        + "</html>"
    ).encode()
    source = source_for(RTF, display_name="a.rtf")

    async def compare() -> None:
        projections: list[list[object]] = []
        for size, chunk in ((1, 1), (batch_size, chunk_size)):
            with pytest.MonkeyPatch.context() as monkeypatch:
                parser, _ = adapter_for(
                    source,
                    monkeypatch,
                    server=FakeTikaServer(content=content, chunk_size=chunk),
                )
                batches = await collect(
                    parser, source, contexts_for(source, RTF, batch_size=size)[1]
                )
                assert batches[-1].manifest is not None
                batches[-1].manifest.validate_batches(batches)
                nodes = {
                    node.node_id: node.model_dump(
                        exclude={
                            "tree_id",
                            "segment_index",
                            "child_start_index",
                            "child_count",
                            "is_last_segment",
                        }
                    )
                    for batch in batches
                    for node in batch.trees
                }
                projections.append(list(nodes.values()))
        assert projections[0] == projections[1]

    asyncio.run(compare())
