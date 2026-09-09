"""Security и resource-limit regressions для JSON format family."""

from __future__ import annotations

import asyncio
import os
import subprocess
import traceback
import urllib.request
from dataclasses import replace
from typing import NoReturn

import pytest
from tests.fakes.parsers import FakeSourceReader
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import ExtractedBatch, JsonPointerLocation, StringScalar
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    JsonDocumentParser,
    JsonLinesParser,
    JsonParserLimits,
)


class _OneByteCountingReader:
    """Отдаёт hostile JSON по одному байту и считает прочитанный prefix."""

    def __init__(self, source_fingerprint: str, content: bytes) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.total_returned = 0

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        returned = self._content[offset : offset + min(size, 1)]
        self.total_returned += len(returned)
        return returned


class _BlockingTailReader:
    """Первый record и bounded lookahead предшествуют блокируемому read."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        prefix_size: int,
    ) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self._prefix_size = prefix_size
        self.tail_allowed = False
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.read_release = asyncio.Event()

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        if offset < self._prefix_size:
            return self._content[offset : min(offset + size, self._prefix_size)]
        if not self.tail_allowed:
            raise AssertionError("JSONL parser запросил tail до первого batch")
        self.read_started.set()
        try:
            await self.read_release.wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        return self._content[offset : offset + size]


async def _parse_document(
    content: bytes,
    *,
    limits: JsonParserLimits,
    max_nesting_depth: int = 16,
    batch_size: int = 1_000,
) -> tuple[ExtractedBatch, ...]:
    source = source_for(
        content, display_name="limits.json", media_type="application/json"
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=batch_size,
        detected_encoding="utf-8",
    )
    context = replace(context, max_nesting_depth=max_nesting_depth)
    return await collect(JsonDocumentParser(limits=limits), source, context)


@pytest.mark.anyio
async def test_global_key_limit_accepts_exact_boundary_and_rejects_plus_one() -> None:
    limits = JsonParserLimits(max_keys=2)

    exact = await _parse_document(b'{"a":1,"nested":{}}', limits=limits)
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b'{"a":1,"nested":{"b":2}}', limits=limits)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "keys"
    assert raised.value.details["limit"] == 2


@pytest.mark.anyio
async def test_value_limit_accepts_exact_boundary_and_rejects_plus_one() -> None:
    limits = JsonParserLimits(max_value_chars=6, max_number_chars=6)

    exact = await _parse_document(b'"abcd"', limits=limits)
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b'"abcde"', limits=limits)

    assert raised.value.details["resource"] == "value_chars"
    assert raised.value.details["limit"] == 6


@pytest.mark.anyio
async def test_value_limit_applies_to_json_literals() -> None:
    exact = await _parse_document(
        b"null",
        limits=JsonParserLimits(max_value_chars=4),
    )
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(
            b"true",
            limits=JsonParserLimits(max_value_chars=3),
        )

    assert raised.value.details["resource"] == "value_chars"
    assert raised.value.details["limit"] == 3


@pytest.mark.anyio
async def test_raw_escape_lexeme_is_limited_even_when_decoded_value_is_short() -> None:
    limits = JsonParserLimits(max_value_chars=7, max_number_chars=7)

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b'"\\u0061"', limits=limits)

    assert raised.value.details["resource"] == "value_chars"
    assert raised.value.details["limit"] == 7


@pytest.mark.anyio
async def test_number_limit_accepts_exact_lexeme_and_rejects_plus_one() -> None:
    limits = JsonParserLimits(max_number_chars=4)

    exact = await _parse_document(b"1234", limits=limits)
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b"12345", limits=limits)

    assert raised.value.details["resource"] == "number_chars"
    assert raised.value.details["limit"] == 4


@pytest.mark.anyio
async def test_record_limit_accepts_exact_document_and_rejects_plus_one() -> None:
    limits = JsonParserLimits(max_record_chars=4)

    exact = await _parse_document(b"1234", limits=limits)
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b"12345", limits=limits)

    assert raised.value.details["resource"] == "record_chars"
    assert raised.value.details["limit"] == 4


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    (
        b"NaN",
        b"Infinity",
        b"-Infinity",
        b'{"value":NaN}',
        b'// comment\n{"value":1}',
        b'{"value":1,}',
        b"[1,]",
    ),
    ids=(
        "nan",
        "infinity",
        "negative-infinity",
        "nested-nan",
        "comment",
        "object-trailing-comma",
        "array-trailing-comma",
    ),
)
async def test_non_standard_json_syntax_is_rejected_strictly(content: bytes) -> None:
    with pytest.raises(ParserError) as raised:
        await _parse_document(content, limits=JsonParserLimits())

    assert raised.value.error_code == "PARSER_MALFORMED_INPUT"
    assert raised.value.details["line_number"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    (b'"\\uD800"', b'"\\uDC00"'),
    ids=("lone-high-surrogate", "lone-low-surrogate"),
)
async def test_lone_json_surrogate_is_rejected(content: bytes) -> None:
    with pytest.raises(ParserError) as raised:
        await _parse_document(content, limits=JsonParserLimits())

    assert raised.value.error_code == "PARSER_MALFORMED_INPUT"
    assert raised.value.details["reason"] == "invalid_unicode_scalar"
    assert raised.value.details["line_number"] == 1


@pytest.mark.anyio
async def test_invalid_utf8_is_a_typed_encoding_error_without_raw_bytes() -> None:
    content = b'{"value":"\xff"}'

    with pytest.raises(ParserError) as raised:
        await _parse_document(content, limits=JsonParserLimits())

    error = raised.value
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            repr(dict(error.details)),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.error_code == "PARSER_ENCODING_UNSUPPORTED"
    assert error.details == {"reason": "decode_failed", "encoding": "utf-8"}
    assert "\\xff" not in rendered


@pytest.mark.anyio
async def test_non_array_document_enforces_max_batch_nodes_during_parse() -> None:
    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(
            b'{"outer":{"leaf":1}}',
            limits=JsonParserLimits(max_batch_nodes=2),
        )

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "batch_nodes"
    assert raised.value.details["limit"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "oversized",
    (b"[ 1234]", b"[1234 ]"),
    ids=("leading-whitespace", "trailing-whitespace"),
)
async def test_top_array_item_record_limit_includes_token_and_whitespace(
    oversized: bytes,
) -> None:
    limits = JsonParserLimits(max_record_chars=4)
    exact = await _parse_document(b"[1234]", limits=limits)
    assert exact[0].record_count == 1

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(oversized, limits=limits)

    assert raised.value.details["resource"] == "record_chars"
    assert raised.value.details["limit"] == 4


@pytest.mark.anyio
async def test_document_record_limit_includes_trailing_whitespace() -> None:
    limits = JsonParserLimits(max_record_chars=4)
    exact = await _parse_document(b"1234", limits=limits)
    assert exact[0].record_count == 1

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(b"1234 ", limits=limits)

    assert raised.value.details["resource"] == "record_chars"
    assert raised.value.details["limit"] == 4


@pytest.mark.anyio
async def test_node_and_array_limits_count_logical_values_at_exact_boundary() -> None:
    exact = await _parse_document(
        b"[0,1,2]",
        limits=JsonParserLimits(max_nodes=4, max_array_items=3),
        batch_size=1,
    )
    assert tuple(batch.record_count for batch in exact) == (1, 1, 1)

    with pytest.raises(SecurityPolicyError) as node_error:
        await _parse_document(
            b"[0,1,2]",
            limits=JsonParserLimits(max_nodes=3, max_array_items=3),
            batch_size=1,
        )
    assert node_error.value.details["resource"] == "nodes"

    with pytest.raises(SecurityPolicyError) as item_error:
        await _parse_document(
            b"[0,1,2,3]",
            limits=JsonParserLimits(max_array_items=3),
        )
    assert item_error.value.details["resource"] == "array_items"


@pytest.mark.anyio
async def test_nesting_limit_accepts_exact_depth_and_rejects_plus_one() -> None:
    exact = await _parse_document(
        b"[0]",
        limits=JsonParserLimits(),
        max_nesting_depth=2,
    )
    assert exact

    with pytest.raises(SecurityPolicyError) as raised:
        await _parse_document(
            b"[[0]]",
            limits=JsonParserLimits(),
            max_nesting_depth=2,
        )

    assert raised.value.details["resource"] == "nesting_depth"
    assert raised.value.details["limit"] == 2


@pytest.mark.anyio
async def test_json_lines_record_limit_is_global_across_batches() -> None:
    content = b'{"id":0}\n{"id":1}\n{"id":2}\n'
    source = source_for(
        content, display_name="records.ndjson", media_type="application/x-ndjson"
    )
    _, base_context = contexts_for(
        source,
        content,
        batch_size=1,
        detected_encoding="utf-8",
    )

    exact = await collect(
        JsonLinesParser(),
        source,
        replace(base_context, max_records=3),
    )
    assert tuple(batch.record_count for batch in exact) == (1, 1, 1)

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(
            JsonLinesParser(),
            source,
            replace(base_context, max_records=2),
        )

    assert raised.value.details["resource"] == "records"
    assert raised.value.details["limit"] == 2


@pytest.mark.anyio
async def test_json_lines_key_limit_is_cumulative_across_batches() -> None:
    content = b'{"a":0}\n{"b":1}\n{"c":2}\n'
    source = source_for(
        content,
        display_name="keys.jsonl",
        media_type="application/jsonl",
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=1,
        detected_encoding="utf-8",
    )

    exact = await collect(
        JsonLinesParser(limits=JsonParserLimits(max_keys=3)),
        source,
        context,
    )
    assert tuple(batch.record_count for batch in exact) == (1, 1, 1)

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(
            JsonLinesParser(limits=JsonParserLimits(max_keys=2)),
            source,
            context,
        )

    assert raised.value.details["resource"] == "keys"
    assert raised.value.details["limit"] == 2


@pytest.mark.anyio
async def test_oversized_string_stops_before_hostile_tail_is_accumulated() -> None:
    content = b'"' + (b"x" * 100_000) + b'"'
    source = source_for(
        content, display_name="hostile.json", media_type="application/json"
    )
    reader = _OneByteCountingReader(source.source_fingerprint, content)
    _, context = contexts_for(
        source,
        content,
        reader=reader,
        detected_encoding="utf-8",
    )
    parser = JsonDocumentParser(
        limits=JsonParserLimits(
            max_value_chars=8,
            max_number_chars=8,
            read_chunk_bytes=4,
        )
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, context)

    assert raised.value.details["resource"] == "value_chars"
    assert reader.total_returned <= 12


@pytest.mark.anyio
async def test_malformed_json_line_reports_exact_line_without_raw_canary() -> None:
    canary = "password=DO_NOT_LEAK_JSON_LINE"
    content = f'{{"ok":1}}\n{{"value":"{canary}"\n{{"unread":3}}\n'.encode()
    source = source_for(
        content, display_name="secret.jsonl", media_type="application/jsonl"
    )
    _, context = contexts_for(source, content, detected_encoding="utf-8")

    with pytest.raises(ParserError) as raised:
        await collect(JsonLinesParser(), source, context)

    error = raised.value
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            repr(dict(error.details)),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.error_code == "PARSER_MALFORMED_INPUT"
    assert error.details["line_number"] == 2
    assert canary not in rendered


@pytest.mark.anyio
async def test_non_json_unicode_whitespace_line_is_not_silently_skipped() -> None:
    content = "1\n\N{NO-BREAK SPACE}\n".encode()
    source = source_for(
        content,
        display_name="unicode-whitespace.jsonl",
        media_type="application/jsonl",
    )
    _, context = contexts_for(source, content, detected_encoding="utf-8")

    with pytest.raises(ParserError) as raised:
        await collect(JsonLinesParser(), source, context)

    assert raised.value.error_code == "PARSER_MALFORMED_INPUT"
    assert raised.value.details["line_number"] == 2
    assert raised.value.details["record_number"] == 2


@pytest.mark.anyio
async def test_registry_preserves_jsonl_malformed_physical_line() -> None:
    prefix = b'{"ok":1}\n{"ok":2}\n'
    canary = "password=DO_NOT_LEAK_REGISTRY_JSONL"
    content = prefix + f'\n{{"value":"{canary}"\n'.encode()
    source = source_for(
        content,
        display_name="registry-malformed.ndjson",
        media_type="application/x-ndjson",
    )
    probe_context, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )
    probe_context = replace(probe_context, max_probe_bytes=len(prefix))
    registry = ParserRegistry()
    registry.register(JsonLinesParser())

    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            async for _batch in stream:
                pass

    error = raised.value
    rendered = "\n".join(
        (
            str(error),
            repr(error),
            repr(dict(error.details)),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.error_code == "PARSER_MALFORMED_INPUT"
    assert error.details["line_number"] == 4
    assert error.details["record_number"] == 3
    assert canary not in rendered


@pytest.mark.anyio
async def test_embedded_code_urls_and_commands_remain_inert_raw_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = (
        "__import__('os').system('touch /tmp/never')",
        "<script>fetch('https://example.invalid')</script>",
        "DROP TABLE users;",
    )
    content = (
        '["' + '","'.join(value.replace('"', '\\"') for value in values) + '"]'
    ).encode()
    source = source_for(
        content, display_name="inert.json", media_type="application/json"
    )
    _, context = contexts_for(source, content, detected_encoding="utf-8")

    def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("JSON parser попытался выполнить embedded content")

    monkeypatch.setattr(os, "system", _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)

    batches = await collect(JsonDocumentParser(), source, context)
    extracted = tuple(
        node.value.raw_value.value
        for batch in batches
        for node in batch.trees
        if node.value is not None
        and isinstance(node.value.raw_value, StringScalar)
        and isinstance(node.location, JsonPointerLocation)
        and node.location.pointer != ""
    )
    assert extracted == values


@pytest.mark.anyio
async def test_json_lines_cancellation_between_batches_closes_pending_read() -> None:
    prefix = b'{"id":0}\n'
    content = prefix + b'{"id":1}\n'
    source = source_for(
        content, display_name="cancel.jsonl", media_type="application/jsonl"
    )
    probe_reader = FakeSourceReader(source.source_fingerprint, content=content)
    parse_reader = _BlockingTailReader(
        source.source_fingerprint,
        content,
        # Line iterator читает один символ следующей строки, чтобы отличить
        # terminal newline, но не накапливает следующую physical record.
        prefix_size=len(prefix) + 1,
    )
    probe_context, parse_context = contexts_for(
        source,
        content,
        batch_size=1,
        reader=probe_reader,
    )
    parse_context = replace(
        parse_context,
        reader=parse_reader,
        detected_encoding="utf-8",
    )
    parser = JsonLinesParser(limits=JsonParserLimits(read_chunk_bytes=len(prefix)))
    registry = ParserRegistry()
    registry.register(parser)
    marker = "password=DO_NOT_LEAK_JSON_CANCELLATION"

    try:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            first = await anext(stream)
            assert first.record_count == 1

            parse_reader.tail_allowed = True
            next_batch = asyncio.create_task(anext(stream))
            await parse_reader.read_started.wait()
            next_batch.cancel(marker)

            with pytest.raises(asyncio.CancelledError) as raised:
                await next_batch
            await parse_reader.read_cancelled.wait()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)
            assert raised.value.args == ()
            assert stream.completed is False
    finally:
        parse_reader.read_release.set()

    assert marker not in "".join(traceback.format_exception(raised.value))
