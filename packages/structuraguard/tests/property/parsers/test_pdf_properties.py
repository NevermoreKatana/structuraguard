"""PDF text layer: page/bbox provenance и отсутствие потерь на границах batches."""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._document_fixtures import pdf_bytes
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import DocumentBlockLocation
from structuraguard.parsers.builtin import PdfParser


@settings(max_examples=10, deadline=None)
@given(
    pages=st.integers(1, 4),
    batch_size=st.integers(1, 4),
    suffix=st.text(alphabet="0123456789ABCDEF", max_size=12),
)
def test_pdf_pages_raw_text_and_boxes_are_invariant_to_batch_size(
    pages: int, batch_size: int, suffix: str
) -> None:
    raw = "raw-001.20-" + suffix
    content = pdf_bytes(text=raw, pages=pages)
    source = source_for(content, display_name="pages.pdf")

    async def compare() -> None:
        projections = []
        for size in (1, batch_size):
            batches = await collect(
                PdfParser(), source, contexts_for(source, content, batch_size=size)[1]
            )
            assert batches[-1].manifest is not None
            batches[-1].manifest.validate_batches(batches)
            lines = [
                line
                for batch in batches
                for block in batch.blocks
                for line in block.lines
            ]
            assert [line.text for line in lines] == [raw] * pages
            for number, line in enumerate(lines, 1):
                assert isinstance(line.location, DocumentBlockLocation)
                assert line.location.page_number == number
                assert line.location.source == source.ref
                assert line.location.bounding_box is not None
            assert sum(batch.record_count or 0 for batch in batches) == pages
            projections.append([(line.text, line.location) for line in lines])
        assert projections[0] == projections[1]

    asyncio.run(compare())
