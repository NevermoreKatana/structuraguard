"""Security regressions для TXT/LOG/Markdown trust boundary."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import traceback
import urllib.request
from collections.abc import AsyncIterator, Callable
from typing import NoReturn

import pytest
from tests.fakes.parsers import FakeSourceReader

from structuraguard.contracts import ExtractedBatch, SourceArtifact
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    LogParser,
    MarkdownParser,
    PlainTextParser,
    TextParserLimits,
    builtin_text_parsers,
)
from structuraguard.ports import Parser
from structuraguard.ports.source import (
    BatchOptions,
    ParseContext,
    ProbeContext,
    SourceReader,
)


def _source(
    content: bytes,
    *,
    display_name: str,
    media_type: str = "text/plain",
) -> SourceArtifact:
    return SourceArtifact(
        artifact_id="source-1",
        display_name=display_name,
        media_type=media_type,
        size_bytes=len(content),
        source_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
    )


def _parse_context(
    source: SourceArtifact,
    reader: SourceReader,
    *,
    batch_size: int = 1,
    max_batches: int = 100,
    max_bytes: int | None = None,
    max_records: int = 1_000,
    max_physical_objects: int = 10_000,
    detected_encoding: str | None = None,
) -> ParseContext:
    return ParseContext(
        reader=reader,
        source_fingerprint=source.source_fingerprint,
        max_bytes=max_bytes or max(1, source.size_bytes),
        max_records=max_records,
        max_nesting_depth=16,
        batch_options=BatchOptions(
            batch_size=batch_size,
            max_batches=max_batches,
        ),
        max_physical_objects=max_physical_objects,
        detected_encoding=detected_encoding,
    )


async def _collect(
    parser: Parser,
    source: SourceArtifact,
    context: ParseContext,
) -> tuple[ExtractedBatch, ...]:
    stream: AsyncIterator[ExtractedBatch] = parser.parse(source, context)
    return tuple([batch async for batch in stream])


class _GuardedReader:
    """Немедленно падает при request или cumulative read за byte budget."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        byte_budget: int,
    ) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.byte_budget = byte_budget
        self.total_returned = 0
        self.reads: list[tuple[int, int, int]] = []

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        assert offset >= 0
        assert size > 0
        assert offset < self.byte_budget
        assert size <= self.byte_budget - offset
        returned = self._content[offset : offset + size]
        self.total_returned += len(returned)
        assert self.total_returned <= self.byte_budget
        self.reads.append((offset, size, len(returned)))
        return returned


class _TailControlledReader:
    """Выдаёт short prefix и запрещает tail до первого batch."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        prefix_size: int,
    ) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.prefix_size = prefix_size
        self.tail_allowed = False
        self.premature_tail_reads = 0

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        if offset >= self.prefix_size and not self.tail_allowed:
            self.premature_tail_reads += 1
            raise AssertionError("Parser запросил tail до первого batch")
        end = offset + size
        if not self.tail_allowed:
            end = min(end, self.prefix_size)
        return self._content[offset:end]


class _ChunkedPrefixReader:
    """Фиксирует chunk boundaries и учитывает обращения к hostile tail."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        prefix_boundaries: tuple[int, ...],
    ) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self._prefix_boundaries = prefix_boundaries
        self._prefix_end = prefix_boundaries[-1]
        self.tail_read_count = 0
        self.tail_reads: list[tuple[int, int, int]] = []

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        if offset >= self._prefix_end:
            self.tail_read_count += 1
            returned = self._content[offset : offset + size]
            self.tail_reads.append((offset, size, len(returned)))
            return returned
        next_boundary = next(
            boundary for boundary in self._prefix_boundaries if boundary > offset
        )
        return self._content[offset : min(offset + size, next_boundary)]


class _BlockingReader:
    """Event-controlled read для cancellation без временных ожиданий."""

    def __init__(self, source_fingerprint: str, content: bytes) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self.read_started = asyncio.Event()
        self.read_release = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.read_count = 0

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        self.read_count += 1
        self.read_started.set()
        try:
            await self.read_release.wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        return self._content[offset : offset + size]


class _OneByteReader:
    """Имитирует hostile fragmentation и фиксирует прочитанный prefix."""

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


class _BlockingAfterPrefixReader(_BlockingReader):
    """Первый short read завершает batch, следующий ждёт cancellation."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        prefix_size: int,
    ) -> None:
        super().__init__(source_fingerprint, content)
        self.prefix_size = prefix_size
        self.block_enabled = False

    async def read(self, *, offset: int, size: int) -> bytes:
        if offset < self.prefix_size:
            self.read_count += 1
            return self._content[offset : min(offset + size, self.prefix_size)]
        if not self.block_enabled:
            raise AssertionError("Parser запросил следующий chunk до первого batch")
        return await super().read(offset=offset, size=size)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("factory", "prefix", "display_name"),
    (
        (PlainTextParser, b"plain text\n", "sample.txt"),
        (MarkdownParser, b"# heading\n\n", "sample.md"),
        (
            LogParser,
            b"2026-09-02 10:45:01 INFO first\n2026-09-02 10:45:02 ERROR second\n",
            "sample.log",
        ),
    ),
    ids=("txt", "markdown", "log"),
)
async def test_probe_never_reads_beyond_max_probe_bytes(
    factory: Callable[[], Parser],
    prefix: bytes,
    display_name: str,
) -> None:
    marker = b"password=DO_NOT_READ_PROBE_TAIL"
    content = prefix + marker
    source = _source(content, display_name=display_name)
    reader = _GuardedReader(
        source.source_fingerprint,
        content,
        byte_budget=len(prefix),
    )
    context = ProbeContext(
        reader=reader,
        source_fingerprint=source.source_fingerprint,
        max_probe_bytes=len(prefix),
    )

    result = await factory().probe(source, context)

    assert result.supported
    assert reader.reads
    assert reader.total_returned <= len(prefix)
    assert all(offset + size <= len(prefix) for offset, size, _ in reader.reads)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("factory", "prefix", "tail", "display_name"),
    (
        (PlainTextParser, b"first\n", b"second\nthird\n", "sample.txt"),
        (MarkdownParser, b"# first\n", b"# second\n# third\n", "sample.md"),
        (
            LogParser,
            b"2026-09-02 10:45:01 INFO first\n2026-09-02 10:45:02 INFO second\n",
            b"2026-09-02 10:45:03 INFO third\n",
            "sample.log",
        ),
    ),
    ids=("txt", "markdown", "log"),
)
async def test_parser_yields_first_complete_batch_before_eof(
    factory: Callable[[], Parser],
    prefix: bytes,
    tail: bytes,
    display_name: str,
) -> None:
    content = prefix + tail
    source = _source(content, display_name=display_name)
    reader = _TailControlledReader(
        source.source_fingerprint,
        content,
        prefix_size=len(prefix),
    )
    context = _parse_context(
        source,
        reader,
        batch_size=1,
        detected_encoding="utf-8",
    )
    stream = factory().parse(source, context)

    first = await anext(stream)

    assert first.record_count == 1
    assert reader.premature_tail_reads == 0
    reader.tail_allowed = True
    remaining = tuple([batch async for batch in stream])
    batches = (first, *remaining)
    assert batches[-1].is_last
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 3


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("factory", "content", "display_name"),
    (
        (PlainTextParser, b"plain text\n", "sample.txt"),
        (MarkdownParser, b"# heading\n", "sample.md"),
        (
            LogParser,
            b"2026-09-02 10:45:01 INFO first\n2026-09-02 10:45:02 INFO second\n",
            "sample.log",
        ),
    ),
    ids=("txt", "markdown", "log"),
)
async def test_cancellation_during_source_read_quarantines_stream_without_sleep(
    factory: Callable[[], Parser],
    content: bytes,
    display_name: str,
) -> None:
    source = _source(content, display_name=display_name)
    probe_reader = FakeSourceReader(source.source_fingerprint, content=content)
    parse_reader = _BlockingReader(source.source_fingerprint, content)
    probe_context = ProbeContext(
        reader=probe_reader,
        source_fingerprint=source.source_fingerprint,
        max_probe_bytes=max(1, len(content)),
    )
    parse_context = _parse_context(source, parse_reader)
    registry = ParserRegistry()
    registry.register(factory())
    marker = "password=DO_NOT_LEAK_READ_CANCELLATION"

    try:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            read_task = asyncio.create_task(anext(stream))
            await parse_reader.read_started.wait()
            read_task.cancel(marker)

            with pytest.raises(asyncio.CancelledError) as raised:
                await read_task
            await parse_reader.read_cancelled.wait()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

            assert raised.value.args == ()
            assert marker not in "".join(traceback.format_exception(raised.value))
            assert stream.completed is False
    finally:
        parse_reader.read_release.set()

    assert parse_reader.read_count == 1


@pytest.mark.anyio
async def test_cancellation_between_batches_uses_event_barrier_not_sleep() -> None:
    content = b"first\nsecond\n"
    prefix = b"first\n"
    source = _source(content, display_name="cancel.txt")
    probe_reader = FakeSourceReader(source.source_fingerprint, content=content)
    parse_reader = _BlockingAfterPrefixReader(
        source.source_fingerprint,
        content,
        prefix_size=len(prefix),
    )
    probe_context = ProbeContext(
        reader=probe_reader,
        source_fingerprint=source.source_fingerprint,
        max_probe_bytes=len(content),
    )
    parse_context = _parse_context(source, parse_reader, batch_size=1)
    registry = ParserRegistry()
    registry.register(PlainTextParser())
    marker = "password=DO_NOT_LEAK_BETWEEN_BATCHES"

    try:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            first = await anext(stream)
            assert first.record_count == 1

            parse_reader.block_enabled = True
            read_task = asyncio.create_task(anext(stream))
            await parse_reader.read_started.wait()
            read_task.cancel(marker)
            with pytest.raises(asyncio.CancelledError) as raised:
                await read_task
            await parse_reader.read_cancelled.wait()
            with pytest.raises(StopAsyncIteration):
                await anext(stream)

            assert raised.value.args == ()
            assert marker not in "".join(traceback.format_exception(raised.value))
            assert stream.completed is False
    finally:
        parse_reader.read_release.set()


@pytest.mark.anyio
async def test_binary_nul_spoof_is_not_selected_as_text_log_or_markdown() -> None:
    content = b"\x00" * 32 + b"# claimed markdown"
    source = _source(
        content,
        display_name="claimed.md",
        media_type="text/markdown",
    )
    reader = FakeSourceReader(source.source_fingerprint, content=content)
    probe_context = ProbeContext(
        reader=reader,
        source_fingerprint=source.source_fingerprint,
        max_probe_bytes=len(content),
    )
    registry = ParserRegistry()
    registry.register_many(builtin_text_parsers())

    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FORMAT"


@pytest.mark.anyio
async def test_line_limit_error_does_not_leak_hostile_raw_content() -> None:
    marker = "password=DO_NOT_LEAK_LINE_LIMIT"
    content = marker.encode()
    source = _source(content, display_name="oversized.txt")
    reader = FakeSourceReader(source.source_fingerprint, content=content)
    context = _parse_context(source, reader)
    parser = PlainTextParser(limits=TextParserLimits(max_line_chars=4))

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, context)

    error = raised.value
    rendered = " ".join(
        (
            str(error),
            repr(error),
            repr(error.details),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert error.details["resource"] == "line_chars"
    assert marker not in rendered


@pytest.mark.anyio
async def test_source_byte_limit_fails_without_reading_hostile_tail() -> None:
    prefix = b"safe"
    marker = b"password=DO_NOT_READ_SOURCE_TAIL"
    content = prefix + marker
    source = _source(content, display_name="oversized.txt")
    reader = _GuardedReader(
        source.source_fingerprint,
        content,
        byte_budget=len(prefix),
    )
    context = _parse_context(source, reader, max_bytes=len(prefix))

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(PlainTextParser(), source, context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "source_bytes"
    assert marker.decode() not in str(raised.value)
    assert reader.total_returned <= len(prefix)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("allowed_prefix", "prefix_boundaries"),
    (
        (b"allowed\n", (len(b"allowed\n"),)),
        (
            b"allowed\r\n",
            (len(b"allowed\r"), len(b"allowed\r\n")),
        ),
    ),
    ids=("lf", "split-crlf"),
)
async def test_line_count_limit_fails_before_reading_hostile_tail(
    allowed_prefix: bytes,
    prefix_boundaries: tuple[int, ...],
) -> None:
    marker = b"password=DO_NOT_READ_LINE_COUNT_TAIL"
    content = allowed_prefix + marker
    source = _source(content, display_name="line-count.txt")
    reader = _ChunkedPrefixReader(
        source.source_fingerprint,
        content,
        prefix_boundaries=prefix_boundaries,
    )
    context = _parse_context(source, reader, detected_encoding="utf-8")
    parser = PlainTextParser(limits=TextParserLimits(max_lines=1))

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "line_count"
    assert reader.tail_read_count == 0
    assert marker.decode() not in str(raised.value)


@pytest.mark.anyio
async def test_split_cr_line_limit_reads_only_one_non_lf_lookahead_byte() -> None:
    allowed_prefix = b"allowed\r"
    marker = b"password=DO_NOT_DECODE_LINE_COUNT_TAIL"
    content = allowed_prefix + marker
    source = _source(content, display_name="split-cr-line-count.txt")
    reader = _ChunkedPrefixReader(
        source.source_fingerprint,
        content,
        prefix_boundaries=(len(allowed_prefix),),
    )
    context = _parse_context(source, reader, detected_encoding="utf-8")
    parser = PlainTextParser(limits=TextParserLimits(max_lines=1))

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "line_count"
    assert reader.tail_reads == [(len(allowed_prefix), 1, 1)]
    assert marker.decode() not in str(raised.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "read_budget", "display_name"),
    (
        (
            MarkdownParser(),
            b"x\n" * 1_000,
            len(b"x\nx\n"),
            "oversized.md",
        ),
        (
            LogParser(),
            b"level=INFO user=a\n" + b"x\n" * 1_000,
            len(b"level=INFO user=a\nx\n"),
            "oversized.log",
        ),
    ),
    ids=("markdown-block", "log-event"),
)
async def test_indivisible_unit_checks_physical_budget_before_accumulating_tail(
    parser: Parser,
    content: bytes,
    read_budget: int,
    display_name: str,
) -> None:
    source = _source(content, display_name=display_name)
    reader = _OneByteReader(source.source_fingerprint, content)
    context = _parse_context(
        source,
        reader,
        max_physical_objects=2 if isinstance(parser, MarkdownParser) else 4,
        detected_encoding="utf-8",
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "physical_objects"
    assert reader.total_returned <= read_budget


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "read_budget", "display_name"),
    (
        (
            MarkdownParser(),
            b"# first\n" + b"x\n" * 1_000,
            len(b"# first\nx\n"),
            "records.md",
        ),
        (
            LogParser(),
            b"level=INFO user=a\nlevel=ERROR user=b\n" + b"x\n" * 1_000,
            len(b"level=INFO user=a\nlevel=ERROR user=b\n"),
            "records.log",
        ),
    ),
    ids=("markdown-block", "log-event"),
)
async def test_indivisible_unit_checks_record_budget_before_accumulating_tail(
    parser: Parser,
    content: bytes,
    read_budget: int,
    display_name: str,
) -> None:
    source = _source(content, display_name=display_name)
    reader = _OneByteReader(source.source_fingerprint, content)
    context = _parse_context(
        source,
        reader,
        max_records=1,
        detected_encoding="utf-8",
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(parser, source, context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "records"
    assert reader.total_returned <= read_budget


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("line_count", "is_allowed"),
    ((1, True), (2, True), (3, False)),
)
async def test_batch_count_limit_is_enforced_at_exact_boundary(
    line_count: int,
    is_allowed: bool,
) -> None:
    content = b"line\n" * line_count
    source = _source(content, display_name="batches.txt")
    reader = FakeSourceReader(source.source_fingerprint, content=content)
    context = _parse_context(
        source,
        reader,
        batch_size=1,
        max_batches=2,
    )

    if is_allowed:
        batches = await _collect(PlainTextParser(), source, context)
        assert len(batches) == line_count
        return

    with pytest.raises(SecurityPolicyError) as raised:
        await _collect(PlainTextParser(), source, context)
    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "batch_count"


@pytest.mark.anyio
async def test_markdown_fence_is_inert_and_kept_as_raw_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_side_effect(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise AssertionError("Parser попытался исполнить или загрузить raw content")

    monkeypatch.setattr(subprocess, "run", fail_side_effect)
    monkeypatch.setattr(subprocess, "Popen", fail_side_effect)
    monkeypatch.setattr(urllib.request, "urlopen", fail_side_effect)
    text = "```sh\ncurl https://invalid.example/DO_NOT_FETCH\n```\n"
    content = text.encode()
    source = _source(content, display_name="hostile.md")
    reader = FakeSourceReader(source.source_fingerprint, content=content)

    batches = await _collect(
        MarkdownParser(),
        source,
        _parse_context(source, reader),
    )

    blocks = tuple(block for batch in batches for block in batch.blocks)
    assert len(blocks) == 1
    assert blocks[0].text == text
