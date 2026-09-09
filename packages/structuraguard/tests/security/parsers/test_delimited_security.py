"""Security regressions для CSV/TSV trust boundary."""

from __future__ import annotations

import asyncio
import csv
import locale
import subprocess
import traceback
import urllib.request
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import NoReturn

import pytest
from tests.fakes.parsers import FakeSourceReader
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts import ExtractedBatch, SourceArtifact, StringScalar
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedParserLimits,
    DelimitedTextParser,
    builtin_text_parsers,
)
from structuraguard.ports.source import ParseContext


def _options() -> DelimitedDetectionOptions:
    return DelimitedDetectionOptions(dialect_override=DelimitedDialect(delimiter=","))


async def _collect(
    parser: DelimitedTextParser,
    source: SourceArtifact,
    context: ParseContext,
) -> tuple[ExtractedBatch, ...]:
    stream: AsyncIterator[ExtractedBatch] = parser.parse(source, context)
    return tuple([batch async for batch in stream])


class _OneByteCountingReader:
    """Отдаёт hostile source по одному байту и считает прочитанный prefix."""

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


class _ExactBoundsReader:
    """Отклоняет requests за объявленной границей source snapshot."""

    def __init__(self, source_fingerprint: str, content: bytes) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.read_count = 0

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        assert 0 <= offset <= len(self._content)
        assert size > 0
        assert offset + size <= len(self._content)
        self.read_count += 1
        return self._content[offset : offset + size]


class _CompletedProbeSampleReader:
    """Сигнализирует, когда весь bounded sample передан dialect detector."""

    def __init__(self, source_fingerprint: str, content: bytes) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.sample_returned = asyncio.Event()

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        returned = self._content[offset : offset + size]
        if offset + len(returned) >= len(self._content):
            self.sample_returned.set()
        return returned


class _BlockingTailReader:
    """Запрещает tail до первого batch и блокирует следующий read."""

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
            raise AssertionError("Parser запросил tail до первого batch")
        self.read_started.set()
        try:
            await self.read_release.wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        return self._content[offset : offset + size]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limits", "content", "resource", "maximum_read"),
    (
        (
            DelimitedParserLimits(
                max_field_size=8,
                read_chunk_bytes=4,
                max_header_probe_rows=1,
            ),
            b'"' + (b"x" * 100_000) + b'",ok\n',
            "field_size",
            16,
        ),
        (
            DelimitedParserLimits(
                max_columns=2,
                read_chunk_bytes=4,
                max_header_probe_rows=1,
            ),
            b"a,b," + (b"HOSTILE_TAIL" * 10_000) + b"\n",
            "columns",
            12,
        ),
        (
            DelimitedParserLimits(
                max_record_chars=8,
                read_chunk_bytes=4,
                max_header_probe_rows=1,
            ),
            b"a," + (b"HOSTILE_RECORD_TAIL" * 10_000) + b"\n",
            "record_chars",
            12,
        ),
    ),
    ids=("field", "columns", "record"),
)
async def test_delimited_limits_stop_before_hostile_tail_is_accumulated(
    limits: DelimitedParserLimits,
    content: bytes,
    resource: str,
    maximum_read: int,
) -> None:
    source = source_for(content, display_name="hostile.csv", media_type="text/csv")
    reader = _OneByteCountingReader(source.source_fingerprint, content)
    _, parse_context = contexts_for(
        source,
        content,
        reader=reader,
        detected_encoding="utf-8",
    )
    parser = DelimitedTextParser(limits=limits, detection_options=_options())

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, parse_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == resource
    assert reader.total_returned <= maximum_read


@pytest.mark.anyio
async def test_max_records_stops_before_next_hostile_record_is_accumulated() -> None:
    content = b"a,b\n" + (b"HOSTILE_RECORD" * 100_000) + b",tail\n"
    source = source_for(content, display_name="records.csv", media_type="text/csv")
    reader = _OneByteCountingReader(source.source_fingerprint, content)
    _, parse_context = contexts_for(
        source,
        content,
        reader=reader,
        detected_encoding="utf-8",
    )
    parse_context = replace(parse_context, max_records=1)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            read_chunk_bytes=4,
            max_header_probe_rows=1,
        ),
        detection_options=_options(),
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, parse_context)

    assert raised.value.details["resource"] == "records"
    assert raised.value.details["limit"] == 1
    assert reader.total_returned <= 5


@pytest.mark.anyio
async def test_malformed_row_error_has_coordinates_and_no_raw_canary() -> None:
    canary = "password=DO_NOT_LEAK_MALFORMED_ROW"
    content = f'key,value\n"{canary},unterminated\n'.encode()
    source = source_for(content, display_name="secret.csv", media_type="text/csv")
    _, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )
    parser = DelimitedTextParser(detection_options=_options())

    with pytest.raises(ParserError) as raised:
        await _collect(parser, source, parse_context)

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
    assert error.details["record_number"] == 2
    assert canary not in rendered


@pytest.mark.anyio
async def test_multiline_malformed_row_reports_physical_failure_line() -> None:
    content = b'key,value\n"first physical line\nsecond physical line'
    source = source_for(content, display_name="multiline.csv", media_type="text/csv")
    _, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )

    with pytest.raises(ParserError) as raised:
        await _collect(
            DelimitedTextParser(detection_options=_options()),
            source,
            parse_context,
        )

    assert raised.value.error_code == "PARSER_MALFORMED_INPUT"
    assert raised.value.details["record_number"] == 2
    assert raised.value.details["line_number"] == 3
    assert raised.value.details["reason"] == "unterminated_quote"


@pytest.mark.anyio
async def test_forced_escape_rejects_invalid_sequence_with_row_and_line() -> None:
    content = b"key,value\nbad\\q,x\n"
    source = source_for(
        content,
        display_name="invalid-escape.csv",
        media_type="text/csv",
    )
    probe_context, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )
    parser = DelimitedTextParser(
        detection_options=DelimitedDetectionOptions(
            dialect_override=DelimitedDialect(
                delimiter=",",
                escape_char="\\",
            )
        )
    )

    with pytest.raises(ParserError) as probe_raised:
        await parser.probe(source, probe_context)
    with pytest.raises(ParserError) as parse_raised:
        await _collect(parser, source, parse_context)

    for error in (probe_raised.value, parse_raised.value):
        assert error.error_code == "PARSER_MALFORMED_INPUT"
        assert error.details == {
            "reason": "invalid_escape",
            "record_number": 2,
            "line_number": 2,
        }


@pytest.mark.anyio
async def test_incomplete_probe_promotes_definitive_quote_failure_without_fallback() -> (
    None
):
    prefix = b'a,b\n"closed"tail,c\n1,2\n'
    canary = "password=DO_NOT_LEAK_INCOMPLETE_PROBE"
    content = prefix + (b"3,4\n" * 100) + f"5,{canary}\n".encode()
    source = source_for(
        content,
        display_name="incomplete-probe.csv",
        media_type="text/csv",
    )
    probe_context, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )
    limits = DelimitedParserLimits(encoding_probe_bytes=len(prefix))

    with pytest.raises(ParserError) as direct_probe_raised:
        await DelimitedTextParser(limits=limits).probe(source, probe_context)

    registry = ParserRegistry()
    registry.register_many(
        (*builtin_text_parsers(), DelimitedTextParser(limits=limits))
    )
    with pytest.raises(ParserError) as selected_raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    with pytest.raises(ParserError) as direct_parse_raised:
        await _collect(
            DelimitedTextParser(limits=limits),
            source,
            parse_context,
        )

    for error in (
        direct_probe_raised.value,
        selected_raised.value,
        direct_parse_raised.value,
    ):
        assert error.error_code == "PARSER_MALFORMED_INPUT"
        assert error.details == {
            "reason": "unexpected_character_after_quote",
            "record_number": 2,
            "line_number": 2,
        }
        rendered = "\n".join(
            (
                str(error),
                repr(error),
                repr(dict(error.details)),
                "".join(traceback.format_exception(error)),
            )
        )
        assert "closed" not in rendered
        assert canary not in rendered


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limits", "prefix", "resource", "limit", "raw_marker"),
    (
        (
            DelimitedParserLimits(max_columns=2),
            b"a,b\n1,2\n3,4,COLUMN_LIMIT_RAW\n",
            "columns",
            2,
            "COLUMN_LIMIT_RAW",
        ),
        (
            DelimitedParserLimits(max_field_size=3),
            b"a,b\n1,FIELD_LIMIT_RAW\n",
            "field_size",
            3,
            "FIELD_LIMIT_RAW",
        ),
        (
            DelimitedParserLimits(max_record_chars=5),
            b"a,b\n1,RECORD_LIMIT_RAW\n",
            "record_chars",
            5,
            "RECORD_LIMIT_RAW",
        ),
    ),
    ids=("columns", "field-size", "record-chars"),
)
async def test_incomplete_probe_propagates_plausible_limits_without_txt_fallback(
    limits: DelimitedParserLimits,
    prefix: bytes,
    resource: str,
    limit: int,
    raw_marker: str,
) -> None:
    tail_canary = "password=DO_NOT_LEAK_LIMIT_TAIL"
    content = prefix + (b"3,4\n" * 100) + f"5,{tail_canary}\n".encode()
    source = source_for(
        content,
        display_name="incomplete-limit.csv",
        media_type="text/csv",
    )
    bounded_limits = replace(limits, encoding_probe_bytes=len(prefix))
    probe_context, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )

    with pytest.raises(SecurityPolicyError) as direct_probe_raised:
        await DelimitedTextParser(limits=bounded_limits).probe(
            source,
            probe_context,
        )

    registry = ParserRegistry()
    registry.register_many(
        (*builtin_text_parsers(), DelimitedTextParser(limits=bounded_limits))
    )
    with pytest.raises(SecurityPolicyError) as selected_raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    with pytest.raises(SecurityPolicyError) as direct_parse_raised:
        await _collect(
            DelimitedTextParser(limits=bounded_limits),
            source,
            parse_context,
        )

    for error in (
        direct_probe_raised.value,
        selected_raised.value,
        direct_parse_raised.value,
    ):
        assert error.error_code == "SECURITY_LIMIT_EXCEEDED"
        assert error.details == {
            "adapter_id": "builtin.delimited",
            "limit": limit,
            "resource": resource,
        }
        rendered = "\n".join(
            (
                str(error),
                repr(error),
                repr(dict(error.details)),
                "".join(traceback.format_exception(error)),
            )
        )
        assert raw_marker not in rendered
        assert tail_canary not in rendered


@pytest.mark.anyio
async def test_shape_hard_cap_reports_the_stricter_caller_column_limit() -> None:
    oversized_row = (b"x," * 10_001) + b"x\n"
    content = b"a,b\n1,2\n" + oversized_row
    source = source_for(content, display_name="wide.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            max_columns=2,
            encoding_probe_bytes=len(content),
            max_dialect_candidates=1,
        ),
        detection_options=DelimitedDetectionOptions(
            delimiters=(",",),
            quote_chars=(None,),
            escape_chars=(None,),
        ),
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await parser.probe(source, probe_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": "builtin.delimited",
        "limit": 2,
        "resource": "columns",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "incomplete_probe", (False, True), ids=("complete", "incomplete")
)
async def test_long_comma_prose_remains_txt_when_dialect_is_not_corroborated(
    incomplete_probe: bool,
) -> None:
    sampled_prose = b"Hello, ordinary prose is intentionally long"
    content = (
        sampled_prose + b" and continues beyond the delimited probe"
        if incomplete_probe
        else sampled_prose
    )
    source = source_for(
        content,
        display_name="claimed.csv",
        media_type="text/csv",
    )
    encoding_probe_bytes = len(b"Hello, ordinary prose") if incomplete_probe else 128
    limits = DelimitedParserLimits(
        max_field_size=8,
        encoding_probe_bytes=encoding_probe_bytes,
    )
    probe_context, _ = contexts_for(source, content)

    result = await DelimitedTextParser(limits=limits).probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None

    registry = ParserRegistry()
    registry.register_many(
        (*builtin_text_parsers(), DelimitedTextParser(limits=limits))
    )
    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "builtin.text"
    assert selected.probe_result.format_id == "txt"


@pytest.mark.anyio
async def test_dominant_single_column_prefix_does_not_promote_partial_field_limit() -> (
    None
):
    prefix = b"one\ntwo\nthree\na,b\nc,LONG_PROSE_FIELD"
    content = prefix + b" continues beyond the incomplete probe boundary"
    source = source_for(
        content,
        display_name="mostly-prose.csv",
        media_type="text/csv",
    )
    limits = DelimitedParserLimits(
        max_field_size=8,
        encoding_probe_bytes=len(prefix),
    )
    probe_context, _ = contexts_for(source, content)

    result = await DelimitedTextParser(limits=limits).probe(source, probe_context)

    assert not result.supported
    assert result.format_id is None

    registry = ParserRegistry()
    registry.register_many(
        (*builtin_text_parsers(), DelimitedTextParser(limits=limits))
    )
    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "builtin.text"
    assert selected.probe_result.format_id == "txt"


@pytest.mark.anyio
async def test_exact_dialect_override_enforces_field_limit_for_comma_prose() -> None:
    content = b"Hello, ordinary prose is intentionally long"
    source = source_for(
        content,
        display_name="forced.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_field_size=8),
        detection_options=_options(),
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await parser.probe(source, probe_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": "builtin.delimited",
        "limit": 8,
        "resource": "field_size",
    }


@pytest.mark.anyio
async def test_delimited_stream_cancellation_between_batches_closes_read() -> None:
    prefix = b"a,b\n"
    content = prefix + b"1,2\n"
    source = source_for(content, display_name="cancel.csv", media_type="text/csv")
    probe_reader = FakeSourceReader(source.source_fingerprint, content=content)
    parse_reader = _BlockingTailReader(
        source.source_fingerprint,
        content,
        prefix_size=len(prefix),
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
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            read_chunk_bytes=len(prefix),
            max_header_probe_rows=1,
        ),
        detection_options=_options(),
    )
    registry = ParserRegistry()
    registry.register(parser)
    marker = "password=DO_NOT_LEAK_CANCELLATION"

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


@pytest.mark.anyio
async def test_expensive_dialect_probe_is_cooperatively_cancellable() -> None:
    row = (b"left" * 256) + b"," + (b"right" * 256) + b"\n"
    content = row * 28
    source = source_for(
        content,
        display_name="expensive.csv",
        media_type="text/csv",
    )
    reader = _CompletedProbeSampleReader(source.source_fingerprint, content)
    probe_context, _ = contexts_for(source, content, reader=reader)
    options = DelimitedDetectionOptions(
        delimiters=(",", "\t", ";", "|", ":", "^", "~", "/"),
        quote_chars=('"', "'"),
        escape_chars=(None, "\\"),
    )
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            encoding_probe_bytes=len(content),
            read_chunk_bytes=len(content),
            max_batch_cells=10_000,
            max_dialect_candidates=32,
        ),
        detection_options=options,
    )

    probe_task = asyncio.create_task(parser.probe(source, probe_context))
    await reader.sample_returned.wait()
    await asyncio.sleep(0)

    assert not probe_task.done()
    assert probe_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe_task


@pytest.mark.anyio
async def test_formula_like_cells_remain_raw_and_inert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    formulae = (
        "=2+2",
        "+SUM(A1:A2)",
        "-cmd|' /C calc'!A0",
        '@HYPERLINK("https://example.invalid")',
    )
    rows = "\n".join(
        f'{index},"{value.replace(chr(34), chr(34) * 2)}"'
        for index, value in enumerate(formulae)
    )
    content = f"id,value\n{rows}\n".encode()
    source = source_for(content, display_name="formula.csv", media_type="text/csv")
    _, parse_context = contexts_for(
        source,
        content,
        detected_encoding="utf-8",
    )

    def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Delimited parser попытался исполнить embedded content")

    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)

    batches = await _collect(
        DelimitedTextParser(detection_options=_options()),
        source,
        parse_context,
    )

    extracted = tuple(
        cell.value.raw_value.value
        for batch in batches
        for table in batch.tables
        for cell in table.cells
        if cell.column_index == 1
        and isinstance(cell.value.raw_value, StringScalar)
        and cell.row_index > 0
    )
    assert extracted == formulae


@pytest.mark.anyio
async def test_delimited_parser_does_not_mutate_csv_or_locale_global_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"a,b\n1,2\n"
    source = source_for(content, display_name="globals.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()

    def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("Delimited parser изменил process-global state")

    monkeypatch.setattr(csv, "field_size_limit", _forbidden)
    monkeypatch.setattr(locale, "setlocale", _forbidden)

    probe = await parser.probe(source, probe_context)
    assert probe.supported
    await _collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )


@pytest.mark.anyio
async def test_delimited_reader_requests_never_cross_source_boundary() -> None:
    content = b"a,b\n1,2\n"
    source = source_for(content, display_name="bounded.csv", media_type="text/csv")
    reader = _ExactBoundsReader(source.source_fingerprint, content)
    probe_context, parse_context = contexts_for(source, content, reader=reader)
    parser = DelimitedTextParser()

    probe = await parser.probe(source, probe_context)
    assert probe.supported
    batches = await _collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert batches[-1].is_last
    assert reader.read_count > 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "max_columns"),
    (
        (b"a,b;c\n1,2;3\n", 1),
        (b"a,b,c;d\n1,2,3;4\n", 2),
    ),
    ids=("one-column-cap", "two-column-cap"),
)
async def test_column_limit_does_not_mask_ambiguous_dialect(
    content: bytes,
    max_columns: int,
) -> None:
    source = source_for(content, display_name="ambiguous.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)

    for parser in (
        DelimitedTextParser(),
        DelimitedTextParser(limits=DelimitedParserLimits(max_columns=max_columns)),
    ):
        with pytest.raises(ParserError) as raised:
            await parser.probe(source, probe_context)

        assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
        assert raised.value.details == {
            "feature": "ambiguous_dialect",
            "reason": "insufficient_structure",
        }

    registry = ParserRegistry()
    registry.register_many(
        (
            *builtin_text_parsers(),
            DelimitedTextParser(limits=DelimitedParserLimits(max_columns=max_columns)),
        )
    )
    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_cross_delimiter_malformed_input_is_not_hidden_by_txt_fallback() -> None:
    content = b'a,b;c\nx,"y,z"tail;d\n1,2;3\n'
    source = source_for(
        content,
        display_name="cross-delimiter.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(ParserError) as raised:
        await DelimitedTextParser().probe(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }

    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), DelimitedTextParser()))
    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_quote_character_ambiguity_is_not_hidden_by_txt_fallback() -> None:
    content = b"\"a\",\"b\"\n'c','d'\n"
    source = source_for(
        content,
        display_name="quote-ambiguity.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), DelimitedTextParser()))

    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_distinct_escape_projections_fail_closed_directly_and_in_registry() -> (
    None
):
    content = b"one\\,two/,three,x\nfour\\,five/,six,y\n"
    source = source_for(
        content,
        display_name="escape-ambiguity.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)
    options = DelimitedDetectionOptions(
        delimiters=(",",),
        quote_chars=(None,),
        escape_chars=("\\", "/"),
    )

    with pytest.raises(ParserError) as direct_raised:
        await DelimitedTextParser(detection_options=options).probe(
            source,
            probe_context,
        )

    assert direct_raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert direct_raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }

    registry = ParserRegistry()
    registry.register_many(
        (
            *builtin_text_parsers(),
            DelimitedTextParser(detection_options=options),
        )
    )
    with pytest.raises(ParserError) as selected_raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert selected_raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert selected_raised.value.details == direct_raised.value.details


@pytest.mark.anyio
async def test_mixed_delimiters_with_malformed_tail_never_fall_back_to_txt() -> None:
    content = b"a,b;c,d\n1,2;3,4\n\"'unterminated,field;stuff\n"
    source = source_for(
        content,
        display_name="mixed-malformed.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(ParserError) as direct_raised:
        await DelimitedTextParser().probe(source, probe_context)

    assert direct_raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert direct_raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }

    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), DelimitedTextParser()))
    with pytest.raises(ParserError) as selected_raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert selected_raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert selected_raised.value.details == direct_raised.value.details


def test_delimited_public_options_are_immutable_and_instance_local() -> None:
    first = DelimitedTextParser()
    second = DelimitedTextParser()

    assert first.limits is not second.limits
    assert first.detection_options is not second.detection_options
    with pytest.raises((AttributeError, TypeError)):
        first.detection_options.delimiters = (";",)  # type: ignore[misc]
    assert second.detection_options.delimiters != (";",)
    assert isinstance(first.limits, DelimitedParserLimits)
    assert isinstance(first.detection_options, DelimitedDetectionOptions)
    assert first.detection_options.dialect_override is None
    assert isinstance(second.detection_options, DelimitedDetectionOptions)
    assert isinstance(second.limits, DelimitedParserLimits)
