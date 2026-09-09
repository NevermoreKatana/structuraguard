"""Unit и contract tests отдельного TXT adapter."""

from __future__ import annotations

import codecs
import tracemalloc
from decimal import Decimal

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

from structuraguard.contracts import PhysicalSample, StringScalar
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import PlainTextParser, TextParserLimits


@pytest.mark.anyio
async def test_plain_text_contract_preserves_exact_lines_and_provenance() -> None:
    content = "alpha\r\nbeta\rgamma\nпоследняя".encode()
    source = source_for(content, display_name="sample.txt")
    probe_context, parse_context = contexts_for(source, content, batch_size=2)

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=PlainTextParser(),
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    lines = tuple(line for batch in batches for line in batch.lines)
    assert tuple(line.text for line in lines) == (
        "alpha",
        "beta",
        "gamma",
        "последняя",
    )
    assert tuple(metadata_map(line.metadata)["line_ending"] for line in lines) == (
        "crlf",
        "cr",
        "lf",
        "none",
    )
    assert tuple(line_span(line.location) for line in lines) == (
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
    )
    assert all(not batch.blocks for batch in batches)
    assert all(batch.record_count == len(batch.lines) for batch in batches)
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 4
    progressive_refs = tuple(ref for batch in batches for ref in batch.indexed_refs)
    assert batches[-1].manifest.source_index.refs == progressive_refs
    assert progressive_refs

    first_line = batches[0].lines[0]
    first_ref = next(ref for ref in progressive_refs if ref.local_id == "line-1")
    sample = PhysicalSample(
        source_ref=first_ref,
        batch_fingerprint=batches[0].batch_fingerprint,
        raw_value=StringScalar(value=first_line.text),
        location=first_line.location,
    )
    assert sample.source_ref in batches[-1].manifest.source_index.refs


@pytest.mark.anyio
async def test_plain_text_empty_source_has_one_empty_terminal_batch() -> None:
    content = b""
    source = source_for(content, display_name="empty.txt")
    probe_context, parse_context = contexts_for(source, content)

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=PlainTextParser(),
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    assert len(batches) == 1
    assert batches[0].is_last
    assert batches[0].lines == ()
    assert batches[0].blocks == ()
    assert batches[0].record_count == 0
    assert batches[0].manifest is not None
    assert batches[0].manifest.record_count == 0


@pytest.mark.anyio
async def test_plain_text_strictly_decodes_utf16_bom() -> None:
    content = codecs.BOM_UTF16_LE + "Привет\r\nмир".encode("utf-16-le")
    source = source_for(content, display_name="utf16.txt")
    probe_context, parse_context = contexts_for(source, content)
    parser = PlainTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(parser, source, parse_context)

    assert probe.supported
    assert probe.detected_encoding is not None
    assert probe.detected_encoding.startswith("utf-16")
    assert tuple(line.text for batch in batches for line in batch.lines) == (
        "Привет",
        "мир",
    )


@pytest.mark.anyio
async def test_plain_text_reports_heuristic_encoding_confidence_and_warning() -> None:
    text = "Это пример русского текста. Строка для определения кодировки. " * 5
    content = text.encode("cp1251")
    source = source_for(content, display_name="legacy.txt")
    probe_context, parse_context = contexts_for(source, content)
    parser = PlainTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(parser, source, parse_context)

    assert probe.detected_encoding == "cp1251"
    assert Decimal("0") < probe.confidence < Decimal("1")
    assert "PARSER_ENCODING_HEURISTIC" in probe.warnings
    assert "PARSER_ENCODING_LOW_CONFIDENCE" in probe.warnings
    assert "".join(line.text for batch in batches for line in batch.lines) == text


@pytest.mark.anyio
@pytest.mark.parametrize("max_chunk_bytes", (1, 2, 4, 8))
async def test_plain_text_short_reads_do_not_commit_to_ascii_prefix_encoding(
    max_chunk_bytes: int,
) -> None:
    text = "INFO " + (
        "Это пример русского текста для определения кодировки без BOM. " * 5
    )
    content = text.encode("cp1251")
    source = source_for(content, display_name="ascii-prefix.txt")
    normal_probe_context, normal_parse_context = contexts_for(source, content)
    short_reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=max_chunk_bytes,
    )
    short_probe_context, short_parse_context = contexts_for(
        source,
        content,
        reader=short_reader,
    )
    parser = PlainTextParser()

    normal_probe = await parser.probe(source, normal_probe_context)
    short_probe = await parser.probe(source, short_probe_context)
    normal_batches = await collect(parser, source, normal_parse_context)
    short_batches = await collect(parser, source, short_parse_context)

    assert normal_probe.detected_encoding == "cp1251"
    assert short_probe.detected_encoding == normal_probe.detected_encoding
    assert physical_projection(short_batches) == physical_projection(normal_batches)


@pytest.mark.anyio
async def test_plain_text_rejects_broken_bom_encoding_with_typed_error() -> None:
    content = codecs.BOM_UTF16_LE + b"\x00"
    source = source_for(content, display_name="broken.txt")
    _, parse_context = contexts_for(source, content)

    with pytest.raises(ParserError) as raised:
        await collect(PlainTextParser(), source, parse_context)

    assert raised.value.error_code == "PARSER_ENCODING_UNSUPPORTED"
    assert raised.value.details["reason"] == "decode_failed"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("line_length", "is_allowed"),
    ((3, True), (4, True), (5, False)),
)
async def test_plain_text_enforces_line_limit_at_exact_boundary(
    line_length: int,
    is_allowed: bool,
) -> None:
    content = b"x" * line_length
    source = source_for(content, display_name="boundary.txt")
    _, parse_context = contexts_for(source, content)
    parser = PlainTextParser(limits=TextParserLimits(max_line_chars=4))

    if is_allowed:
        batches = await collect(parser, source, parse_context)
        assert tuple(line.text for batch in batches for line in batch.lines) == (
            "x" * line_length,
        )
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)
    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "line_chars"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("line_count", "is_allowed"),
    ((1, True), (2, True), (3, False)),
)
async def test_plain_text_enforces_line_count_before_next_line_is_retained(
    line_count: int,
    is_allowed: bool,
) -> None:
    content = b"line\n" * line_count
    source = source_for(content, display_name="line-count.txt")
    _, parse_context = contexts_for(source, content)
    parser = PlainTextParser(limits=TextParserLimits(max_lines=2))

    if is_allowed:
        batches = await collect(parser, source, parse_context)
        assert sum(batch.record_count or 0 for batch in batches) == line_count
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)
    assert raised.value.details["resource"] == "line_count"


@pytest.mark.anyio
async def test_plain_text_chunk_and_batch_boundaries_do_not_change_projection() -> None:
    content = "один\r\nдва\nтри\rчетыре".encode()
    source = source_for(content, display_name="batches.txt")
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

    single = await collect(PlainTextParser(), source, single_context)
    bulk = await collect(PlainTextParser(), source, bulk_context)

    assert physical_projection(single) == physical_projection(bulk)


@pytest.mark.anyio
async def test_one_byte_reader_preserves_long_unicode_line_without_fragments() -> None:
    text = "Ж" * 100_000
    content = text.encode()
    source = source_for(content, display_name="fragmented.txt")
    reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=1,
    )
    _, parse_context = contexts_for(
        source,
        content,
        reader=reader,
        detected_encoding="utf-8",
    )

    tracemalloc.start()
    try:
        batches = await collect(PlainTextParser(), source, parse_context)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    lines = tuple(line for batch in batches for line in batch.lines)
    assert tuple(line.text for line in lines) == (text,)
    assert peak_bytes < 3_000_000
