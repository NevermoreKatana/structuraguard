"""A: Unicode, BOM, physical newlines и batches проверяются независимым raw oracle."""

from __future__ import annotations

import asyncio
import codecs
import re
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    metadata_map,
    physical_projection,
    source_for,
)

from structuraguard.contracts import LineRangeLocation
from structuraguard.parsers.builtin import LogParser, MarkdownParser, PlainTextParser


@pytest.mark.parametrize("format_id", ["txt", "log", "md"])
@settings(max_examples=30, deadline=None)
@given(
    values=st.lists(
        st.text(alphabet=st.characters(blacklist_categories=("Cc", "Cs")), max_size=24),
        min_size=2,
        max_size=7,
    ),
    ending=st.sampled_from(["\n", "\r\n", "\r"]),
    encoding=st.sampled_from(["utf-8", "utf-16-be", "utf-32-le"]),
    chunk=st.integers(1, 19),
    batch_size=st.integers(1, 4),
    final_ending=st.booleans(),
)
def test_text_raw_provenance_is_invariant_to_unicode_encoding_and_batch_boundaries(
    format_id: Literal["txt", "log", "md"],
    values: list[str],
    ending: str,
    encoding: str,
    chunk: int,
    batch_size: int,
    final_ending: bool,
) -> None:
    lines = values
    if format_id == "log":
        lines = [
            f"2026-09-02 10:45:0{i} INFO message {value}"
            for i, value in enumerate(values)
        ]
    elif format_id == "md":
        lines = ["# Heading", "", "```text", *values, "```"]
    raw = ending.join(lines) + (ending if final_ending else "")
    bom = {
        "utf-8": codecs.BOM_UTF8,
        "utf-16-be": codecs.BOM_UTF16_BE,
        "utf-32-le": codecs.BOM_UTF32_LE,
    }[encoding]
    content = bom + raw.encode(encoding)
    source = source_for(content, display_name=f"a.{format_id}")
    expected = [
        match[0] for match in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", raw) if match[0]
    ]

    async def compare() -> None:
        projections = []
        for size, read_size in ((1, 1), (batch_size, chunk)):
            parser = {"txt": PlainTextParser, "log": LogParser, "md": MarkdownParser}[
                format_id
            ]()
            reader = ShortReadSourceReader(
                source.source_fingerprint, content, max_chunk_bytes=read_size
            )
            batches = await collect(
                parser,
                source,
                contexts_for(source, content, reader=reader, batch_size=size)[1],
            )
            assert batches[-1].manifest is not None
            batches[-1].manifest.validate_batches(batches)
            actual_lines = [line for batch in batches for line in batch.lines]
            assert len(actual_lines) == len(expected)
            rebuilt = []
            for number, line in enumerate(actual_lines, 1):
                suffix = {"lf": "\n", "crlf": "\r\n", "cr": "\r", "none": ""}[
                    str(metadata_map(line.metadata)["line_ending"])
                ]
                rebuilt.append(line.text + suffix)
                assert isinstance(line.location, LineRangeLocation)
                assert line.location.source == source.ref
                assert (
                    line.line_number,
                    line.location.line_start,
                    line.location.line_end,
                ) == (number, number, number)
                # Whole-line provenance не заявляет sub-line span; columns нужны captures.
                assert (line.location.column_start, line.location.column_end) == (
                    None,
                    None,
                )
            assert rebuilt == expected
            assert "".join(rebuilt) == raw
            if format_id != "txt":
                assert (
                    "".join(
                        block.text or "" for batch in batches for block in batch.blocks
                    )
                    == raw
                )
            projections.append(physical_projection(batches))
        assert projections[0] == projections[1]

    asyncio.run(compare())
