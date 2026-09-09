"""Unicode и batch invariance на небольших воспроизводимых OOXML fixtures."""

from __future__ import annotations

import asyncio
from html import escape
from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._document_fixtures import package_parts, zip_bytes
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    source_for,
)

from structuraguard.parsers.builtin import DocxParser, XlsxParser


@settings(max_examples=12, deadline=None)
@given(
    st.sampled_from(["xlsx", "docx"]),
    st.lists(
        st.text(
            alphabet=st.characters(
                blacklist_categories=("Cc", "Cs"),
                blacklist_characters=("\ufffe", "\uffff"),
            ),
            max_size=16,
        ),
        min_size=1,
        max_size=7,
    ),
    st.integers(1, 3),
)
def test_document_unicode_batch_and_short_read_invariance(
    format_id: Literal["xlsx", "docx"], values: list[str], batch_size: int
) -> None:
    parts = package_parts(format_id)
    if format_id == "xlsx":
        rows = "".join(
            f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{escape(value)}</t></is></c></row>'
            for index, value in enumerate(values, 1)
        )
        parts["xl/worksheets/sheet1.xml"] = (
            f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>'.encode()
        )
    else:
        paragraphs = "".join(
            f"<w:p><w:r><w:t>{escape(value)}</w:t></w:r></w:p>" for value in values
        )
        parts["word/document.xml"] = (
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{paragraphs}</w:body></w:document>'.encode()
        )
    content = zip_bytes(parts)
    source = source_for(content, display_name=f"a.{format_id}")

    async def compare() -> None:
        for size in (1, batch_size, 100):
            reader = ShortReadSourceReader(
                source.source_fingerprint, content, max_chunk_bytes=17
            )
            parser = XlsxParser() if format_id == "xlsx" else DocxParser()
            batches = await collect(
                parser,
                source,
                contexts_for(source, content, batch_size=size, reader=reader)[1],
            )
            assert batches[-1].manifest is not None
            batches[-1].manifest.validate_batches(batches)
            actual = (
                [
                    c.value.raw_value.value
                    for batch in batches
                    for table in batch.tables
                    for c in table.cells
                ]
                if format_id == "xlsx"
                else [block.text for batch in batches for block in batch.blocks]
            )
            assert actual == values
            assert sum(batch.record_count or 0 for batch in batches) == len(values)

    asyncio.run(compare())
