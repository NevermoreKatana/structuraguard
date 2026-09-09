"""Marked sections сохраняют единый контракт при изменении HTML dispatch CPython."""

from __future__ import annotations

import traceback

import pytest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import (
    CssSelectorLocation,
    ExtractedBatch,
    PhysicalNodeKind,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import HtmlParser, HtmlParserLimits

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("chunk_size", (1, 2, 3, 8, 4096))
@pytest.mark.parametrize("batch_size", (1, 2))
@pytest.mark.parametrize("keyword", ("bogus", "BOGUS"))
async def test_unknown_marked_section_is_typed_across_chunk_and_batch_boundaries(
    chunk_size: int, batch_size: int, keyword: str
) -> None:
    content = f"<p>prefix</p>\n<p>\n<![{keyword} secret-canary]>".encode()
    source = source_for(content, display_name="malformed.html")
    probe, parse = contexts_for(source, content, batch_size=batch_size)
    parser = HtmlParser(limits=HtmlParserLimits(read_chunk_bytes=chunk_size))
    emitted: list[ExtractedBatch] = []

    with pytest.raises(ParserError) as failure:
        await parser.probe(source, probe)
    assert failure.value.error_code == "PARSER_MALFORMED_INPUT"
    assert failure.value.details["line_number"] == 3
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))

    with pytest.raises(ParserError) as failure:
        async for batch in parser.parse(source, parse):
            emitted.append(batch)
    assert failure.value.error_code == "PARSER_MALFORMED_INPUT"
    assert failure.value.details["line_number"] == 3
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert all(not batch.is_last and batch.manifest is None for batch in emitted)


@pytest.mark.parametrize("chunk_size", (1, 2, 3, 8, 4096))
async def test_supported_marked_sections_keep_raw_values_and_provenance(
    chunk_size: int,
) -> None:
    content = b"<p>\n<![CDATA[raw]]>\n<![if IE]>\n<![endif]></p>"
    source = source_for(content, display_name="marked.html")
    probe, parse = contexts_for(source, content, batch_size=1)
    parser = HtmlParser(limits=HtmlParserLimits(read_chunk_bytes=chunk_size))
    assert (await parser.probe(source, probe)).supported
    batches = await collect(parser, source, parse)
    manifest = batches[-1].manifest
    assert manifest is not None
    manifest.validate_batches(batches)
    declarations = [
        node
        for batch in batches
        for node in batch.trees
        if node.node_kind is PhysicalNodeKind.DECLARATION
    ]
    assert [node.value.raw_value.value for node in declarations if node.value] == [
        "CDATA[raw",
        "if IE",
        "endif",
    ]
    for node, line in zip(declarations, (2, 3, 4), strict=True):
        assert isinstance(node.location, CssSelectorLocation)
        assert node.location.line_number == line
        assert node.location.column_number == 0


@pytest.mark.parametrize("chunk_size", (1, 3, 4096))
@pytest.mark.parametrize(
    "content",
    (
        b'<p title="<![bogus]>">text</p>',
        b"<p><!-- <![bogus]> --></p>",
        b'<script>const data = "<![bogus]>";</script>',
        b'<style>p::before { content: "<![bogus]>"; }</style>',
    ),
    ids=("attribute", "comment", "script", "style"),
)
async def test_marked_section_lookalike_inside_inert_data_is_preserved(
    chunk_size: int, content: bytes
) -> None:
    source = source_for(content, display_name="inert.html")
    probe, parse = contexts_for(source, content)
    parser = HtmlParser(limits=HtmlParserLimits(read_chunk_bytes=chunk_size))
    assert (await parser.probe(source, probe)).supported
    batches = await collect(parser, source, parse)
    manifest = batches[-1].manifest
    assert manifest is not None
    manifest.validate_batches(batches)
    assert any(
        node.value is not None and "<![bogus]>" in str(node.value.raw_value.value)
        for batch in batches
        for node in batch.trees
    )


@pytest.mark.parametrize("chunk_size", (1, 3))
async def test_unterminated_marked_section_keeps_token_limit(chunk_size: int) -> None:
    content = b"<p><![CDATA[" + b"x" * 100
    source = source_for(content, display_name="limit.html")
    parser = HtmlParser(
        limits=HtmlParserLimits(read_chunk_bytes=chunk_size, max_token_chars=32)
    )
    with pytest.raises(SecurityPolicyError) as failure:
        await collect(parser, source, contexts_for(source, content)[1])
    assert failure.value.error_code == "SECURITY_LIMIT_EXCEEDED"
