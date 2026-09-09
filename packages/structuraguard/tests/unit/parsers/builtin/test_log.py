"""Unit и contract tests отдельного LOG adapter."""

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

from structuraguard.contracts import LineRangeLocation, StringScalar
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import LogParser, LogParserLimits

_FIRST_EVENT = (
    "2026-09-02 10:45:01 ERROR user_id=15 request failed\r\n"
    "    Traceback: bounded continuation\n"
)
_SECOND_EVENT = "2026-09-02 10:45:02 INFO request complete\n"


@pytest.mark.anyio
async def test_log_contract_preserves_events_captures_and_exact_spans() -> None:
    content = (_FIRST_EVENT + _SECOND_EVENT).encode()
    source = source_for(content, display_name="service.log")
    probe_context, parse_context = contexts_for(source, content, batch_size=1)

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=LogParser(),
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    lines = tuple(line for batch in batches for line in batch.lines)
    blocks = tuple(block for batch in batches for block in batch.blocks)
    assert tuple(line.text for line in lines) == (
        "2026-09-02 10:45:01 ERROR user_id=15 request failed",
        "    Traceback: bounded continuation",
        "2026-09-02 10:45:02 INFO request complete",
    )
    assert tuple(metadata_map(line.metadata)["line_ending"] for line in lines) == (
        "crlf",
        "lf",
        "lf",
    )
    assert tuple(block.text for block in blocks) == (_FIRST_EVENT, _SECOND_EVENT)
    assert tuple(
        (
            metadata_map(block.metadata)["physical_kind"],
            metadata_map(block.metadata)["recognizer"],
        )
        for block in blocks
    ) == (("log_event", "iso"), ("log_event", "iso"))
    assert tuple(line_span(block.location) for block in blocks) == ((1, 2), (3, 3))
    assert all(block.lines == () for block in blocks)
    assert sum(batch.record_count or 0 for batch in batches) == 2
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 2

    first_values = blocks[0].values
    assert tuple(value.technical_type_hint for value in first_values) == (
        "timestamp",
        "log_level",
        "key_value",
    )
    assert tuple(value.raw_value for value in first_values) == (
        StringScalar(value="2026-09-02 10:45:01"),
        StringScalar(value="ERROR"),
        StringScalar(value="user_id=15"),
    )
    first_line = lines[0].text
    for value in first_values:
        location = value.location
        assert isinstance(location, LineRangeLocation)
        assert location.line_start == location.line_end == 1
        assert location.column_start is not None
        assert location.column_end is not None
        assert isinstance(value.raw_value, StringScalar)
        assert (
            first_line[location.column_start : location.column_end]
            == value.raw_value.value
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "display_name"),
    (
        (b"", "empty.log"),
        (b"2026-09-02 10:45:01 INFO only one line\n", "single.log"),
        (b"arbitrary first row\narbitrary second row\n", "unknown.log"),
        (b'{"level":"INFO"}\n{"level":"ERROR"}\n', "events.log"),
    ),
)
async def test_log_probe_declines_empty_ambiguous_and_json_lines(
    content: bytes,
    display_name: str,
) -> None:
    source = source_for(content, display_name=display_name)
    probe_context, _ = contexts_for(source, content)

    result = await LogParser().probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None


@pytest.mark.anyio
async def test_log_probe_does_not_guess_key_values_embedded_in_prose() -> None:
    content = b"Please set level=INFO user=alice\nPlease set level=ERROR user=bob\n"
    source = source_for(content, display_name="instructions.txt")
    probe_context, _ = contexts_for(source, content)

    result = await LogParser().probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None


@pytest.mark.anyio
async def test_log_probe_applies_capture_limit_only_after_known_kv_structure() -> None:
    tokens = ["level=INFO"]
    tokens.extend(f"field{index}=x" for index in range(64))
    line = " ".join(tokens) + "\n"
    content = (line + line).encode()
    source = source_for(content, display_name="oversized-kv.log")
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(SecurityPolicyError) as raised:
        await LogParser().probe(source, probe_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "capture_groups"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("line", "max_capture_groups"),
    (
        (b"2026-09-02 10:45:01 INFO event\n", 1),
        (
            b'127.0.0.1 - - [10/Oct/2000:13:55:36 -0700] "GET / HTTP/1.0" 200 1\n',
            4,
        ),
    ),
    ids=("iso", "apache-combined"),
)
async def test_log_probe_defers_fixed_capture_limit_until_structure_repeats(
    line: bytes,
    max_capture_groups: int,
) -> None:
    parser = LogParser(limits=LogParserLimits(max_capture_groups=max_capture_groups))
    single_content = line + b"ordinary prose\n"
    single_source = source_for(single_content, display_name="single.log")
    single_context, _ = contexts_for(single_source, single_content)

    single_result = await parser.probe(single_source, single_context)

    assert not single_result.supported

    repeated_content = line * 2
    repeated_source = source_for(repeated_content, display_name="repeated.log")
    repeated_context, _ = contexts_for(repeated_source, repeated_content)
    with pytest.raises(SecurityPolicyError) as raised:
        await parser.probe(repeated_source, repeated_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "capture_groups"


@pytest.mark.anyio
async def test_log_probe_does_not_treat_unicode_separators_as_event_boundaries() -> (
    None
):
    content = (
        "2026-09-02 10:45:01 INFO first"
        "\u2028"
        "2026-09-02 10:45:02 ERROR second"
        "\u2029"
        "2026-09-02 10:45:03 INFO third"
    ).encode()
    source = source_for(content, display_name="unicode-separators.log")
    probe_context, _ = contexts_for(source, content)

    result = await LogParser().probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("value_length", "is_supported"),
    ((1_024, True), (1_025, False)),
)
async def test_log_key_value_recognizer_requires_complete_bounded_token(
    value_length: int,
    is_supported: bool,
) -> None:
    line = f"level=INFO payload={'x' * value_length}\n"
    content = (line + line).encode()
    source = source_for(content, display_name="key-value-boundary.log")
    probe_context, _ = contexts_for(source, content)

    result = await LogParser().probe(source, probe_context)

    assert result.supported is is_supported


@pytest.mark.anyio
async def test_log_probe_declines_json_lines_with_oversized_integer() -> None:
    oversized_integer = "9" * 5_000
    content = (
        f'{{"sequence":{oversized_integer}}}\n{{"sequence":{oversized_integer}}}\n'
    ).encode()
    source = source_for(content, display_name="oversized-integer.log")
    probe_context, _ = contexts_for(source, content)

    result = await LogParser().probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None


@pytest.mark.anyio
async def test_log_detects_bounded_apache_combined_structure() -> None:
    line = (
        "127.0.0.1 - frank [10/Oct/2000:13:55:36 -0700] "
        '"GET /apache_pb.gif HTTP/1.0" 200 2326\n'
    )
    content = (line + line).encode()
    source = source_for(content, display_name="access.log")
    probe_context, parse_context = contexts_for(source, content)
    parser = LogParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(parser, source, parse_context)

    assert probe.supported
    assert probe.format_id == "log"
    assert {
        metadata_map(block.metadata)["recognizer"]
        for batch in batches
        for block in batch.blocks
    } == {"apache_combined"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event_lines", "is_allowed"),
    ((1, True), (2, True), (3, False)),
)
async def test_log_enforces_multiline_event_limit_at_exact_boundary(
    event_lines: int,
    is_allowed: bool,
) -> None:
    lines = ["2026-09-02 10:45:01 ERROR failure"]
    lines.extend(f"    continuation-{index}" for index in range(event_lines - 1))
    content = ("\n".join(lines) + "\n").encode()
    source = source_for(content, display_name="boundary.log")
    _, parse_context = contexts_for(source, content)
    parser = LogParser(limits=LogParserLimits(max_event_lines=2))

    if is_allowed:
        batches = await collect(parser, source, parse_context)
        blocks = tuple(block for batch in batches for block in batch.blocks)
        assert len(blocks) == 1
        assert line_span(blocks[0].location)[1] == event_lines
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)
    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "event_lines"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("event_length", "is_allowed"),
    ((3, True), (4, True), (5, False)),
)
async def test_log_enforces_event_char_limit_at_exact_boundary(
    event_length: int,
    is_allowed: bool,
) -> None:
    content = b"x" * event_length
    source = source_for(content, display_name="event-size.log")
    _, parse_context = contexts_for(source, content)
    parser = LogParser(limits=LogParserLimits(max_event_chars=4))

    if is_allowed:
        batches = await collect(parser, source, parse_context)
        assert sum(batch.record_count or 0 for batch in batches) == 1
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)
    assert raised.value.details["resource"] == "event_chars"


@pytest.mark.anyio
async def test_log_enforces_capture_group_limit_before_yielding_event() -> None:
    parser = LogParser(limits=LogParserLimits(max_capture_groups=2))
    allowed_content = b"2026-09-02 10:45:01 INFO complete\n"
    allowed_source = source_for(allowed_content, display_name="allowed.log")
    _, allowed_context = contexts_for(allowed_source, allowed_content)

    allowed_batches = await collect(parser, allowed_source, allowed_context)

    allowed_values = tuple(
        value
        for batch in allowed_batches
        for block in batch.blocks
        for value in block.values
    )
    assert len(allowed_values) == 2

    rejected_content = b"2026-09-02 10:45:01 INFO user_id=15 rejected\n"
    rejected_source = source_for(rejected_content, display_name="rejected.log")
    _, rejected_context = contexts_for(rejected_source, rejected_content)
    stream = parser.parse(rejected_source, rejected_context)

    with pytest.raises(SecurityPolicyError) as raised:
        await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "capture_groups"


@pytest.mark.anyio
async def test_log_chunk_and_batch_boundaries_do_not_split_or_change_events() -> None:
    content = (_FIRST_EVENT + _SECOND_EVENT).encode()
    source = source_for(content, display_name="batches.log")
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

    single = await collect(LogParser(), source, single_context)
    bulk = await collect(LogParser(), source, bulk_context)

    assert physical_projection(single) == physical_projection(bulk)


@pytest.mark.anyio
async def test_log_rejects_stateful_codec_with_typed_error() -> None:
    content = b"hello\n\x1b(B"
    source = source_for(content, display_name="stateful.log")
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
        await collect(LogParser(), source, parse_context)

    assert raised.value.error_code == "PARSER_ENCODING_UNSUPPORTED"
    assert raised.value.details["reason"] == "unsupported_codec"
