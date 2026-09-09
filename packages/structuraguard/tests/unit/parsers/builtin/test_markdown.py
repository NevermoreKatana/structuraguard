"""Unit и contract tests отдельного Markdown adapter."""

from __future__ import annotations

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    line_span,
    metadata_map,
    physical_projection,
    source_for,
)

from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import MarkdownParser, MarkdownParserLimits

_MARKDOWN = "# Заголовок\r\n\r\nАбзац\n- пункт\n```sh\necho DO_NOT_EXECUTE\n```\n"


@pytest.mark.anyio
async def test_markdown_contract_preserves_ordered_blocks_and_exact_text() -> None:
    content = _MARKDOWN.encode()
    source = source_for(content, display_name="readme.md", media_type="text/markdown")
    probe_context, parse_context = contexts_for(source, content, batch_size=1)

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=MarkdownParser(),
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    lines = tuple(line for batch in batches for line in batch.lines)
    blocks = tuple(block for batch in batches for block in batch.blocks)
    assert tuple(line.text for line in lines) == (
        "# Заголовок",
        "",
        "Абзац",
        "- пункт",
        "```sh",
        "echo DO_NOT_EXECUTE",
        "```",
    )
    assert tuple(metadata_map(line.metadata)["line_ending"] for line in lines) == (
        "crlf",
        "crlf",
        "lf",
        "lf",
        "lf",
        "lf",
        "lf",
    )
    assert tuple(metadata_map(block.metadata)["physical_kind"] for block in blocks) == (
        "heading",
        "blank",
        "paragraph",
        "list",
        "fenced_code",
    )
    assert tuple(block.text for block in blocks) == (
        "# Заголовок\r\n",
        "\r\n",
        "Абзац\n",
        "- пункт\n",
        "```sh\necho DO_NOT_EXECUTE\n```\n",
    )
    assert tuple(line_span(block.location) for block in blocks) == (
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
        (5, 7),
    )
    assert all(block.lines == () and block.values == () for block in blocks)
    assert sum(batch.record_count or 0 for batch in batches) == 5
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 5


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    (b"", b"ordinary prose without structural markers\n"),
)
async def test_markdown_probe_declines_empty_and_ambiguous_prose(
    content: bytes,
) -> None:
    source = source_for(content, display_name="claimed.md", media_type="text/markdown")
    probe_context, _ = contexts_for(source, content)

    result = await MarkdownParser().probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None


@pytest.mark.anyio
async def test_markdown_unclosed_fence_remains_inert_physical_block_to_eof() -> None:
    text = "```python\nraise RuntimeError('DO_NOT_EXECUTE')"
    content = text.encode()
    source = source_for(content, display_name="unclosed.md")
    _, parse_context = contexts_for(source, content)

    batches = await collect(MarkdownParser(), source, parse_context)
    blocks = tuple(block for batch in batches for block in batch.blocks)

    assert len(blocks) == 1
    assert blocks[0].text == text
    assert metadata_map(blocks[0].metadata)["physical_kind"] == "fenced_code"
    assert line_span(blocks[0].location) == (1, 2)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("block_length", "is_allowed"),
    ((3, True), (4, True), (5, False)),
)
async def test_markdown_enforces_block_limit_at_exact_boundary(
    block_length: int,
    is_allowed: bool,
) -> None:
    content = b"x" * block_length
    source = source_for(content, display_name="boundary.md")
    _, parse_context = contexts_for(source, content)
    parser = MarkdownParser(limits=MarkdownParserLimits(max_block_chars=4))

    if is_allowed:
        batches = await collect(parser, source, parse_context)
        blocks = tuple(block for batch in batches for block in batch.blocks)
        assert tuple(block.text for block in blocks) == ("x" * block_length,)
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)
    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "block_chars"


@pytest.mark.anyio
async def test_markdown_chunk_and_batch_boundaries_do_not_change_blocks() -> None:
    content = _MARKDOWN.encode()
    source = source_for(content, display_name="batches.md")
    short_reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=1,
    )
    _, single_context = contexts_for(
        source,
        content,
        batch_size=1,
        reader=short_reader,
    )
    _, bulk_context = contexts_for(source, content, batch_size=100)

    single = await collect(MarkdownParser(), source, single_context)
    bulk = await collect(MarkdownParser(), source, bulk_context)

    assert physical_projection(single) == physical_projection(bulk)


@pytest.mark.anyio
async def test_markdown_rejects_stateful_codec_with_typed_error() -> None:
    content = b"hello\n\x1b(B"
    source = source_for(content, display_name="stateful.md")
    reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=len(b"hello\n"),
    )
    _, parse_context = contexts_for(
        source,
        content,
        reader=reader,
        detected_encoding="iso2022-jp",
    )

    with pytest.raises(ParserError) as raised:
        await collect(MarkdownParser(), source, parse_context)

    assert raised.value.error_code == "PARSER_ENCODING_UNSUPPORTED"
    assert raised.value.details["reason"] == "unsupported_codec"
