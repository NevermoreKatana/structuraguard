"""Общая policy: отказ до I/O/накопления, точная граница и safe audit."""

import asyncio
import traceback
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.unit.llm.test_router import Clock
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts.security import Resource, SecurityLimits, SecurityPolicy
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.builtin import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedParserLimits,
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
    TextParserLimits,
)
from structuraguard.security import SecuritySession


def session(**limits: int) -> SecuritySession:
    return SecuritySession(
        SecurityPolicy(
            limits=SecurityLimits(**limits),
            allowed_formats=("txt", "csv", "json"),
            parser_trust="trusted",
        ),
        run_id=UUID(int=1),
        clock=lambda: datetime(2026, 9, 13, tzinfo=UTC),
    )


@pytest.mark.parametrize("field", tuple(SecurityLimits.model_fields))
@pytest.mark.parametrize("bad", [0, -1, True, "1", float("inf")])
def test_limits_reject_invalid_values(field: str, bad: object) -> None:
    with pytest.raises(ValidationError):
        SecurityLimits.model_validate({field: bad})


@given(st.integers(min_value=1, max_value=1000))
def test_exact_reservation_and_plus_one_are_atomic(maximum: int) -> None:
    run = session(max_db_queries=maximum)
    run.reserve(Resource.DB_QUERIES, maximum)
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        run.reserve(Resource.DB_QUERIES, 1)
    assert run.used(Resource.DB_QUERIES) == maximum
    assert len(run.events) == 1
    assert run.events[0].resource is Resource.DB_QUERIES
    assert run.events[0].limit == maximum
    with pytest.raises(SecurityPolicyError):
        run.reserve(Resource.DB_QUERIES, 0)
    assert len(run.events) == 1


def test_policy_can_only_narrow_and_forged_input_does_not_serialize() -> None:
    run = session(max_records=5)
    assert run.policy.narrow(SecurityLimits(max_records=4)).limits.max_records == 4
    with pytest.raises(ValueError):
        run.policy.narrow(SecurityLimits(max_records=6))
    bad = run.policy.model_copy(update={"limits": "password=secret-canary"})
    with pytest.raises(SecurityPolicyError) as error:
        SecuritySession(bad, run_id=UUID(int=1))
    assert "secret-canary" not in str(error.value)


class Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.sizes: list[int] = []
        self.offset = 0

    async def read(self, size: int) -> bytes:
        self.sizes.append(size)
        chunk = self.data[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["file", "stream"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_source_bytes_check_before_buffer_extension(
    kind: str, extra: int
) -> None:
    run = session(max_file_bytes=4, max_stream_bytes=4, read_chunk_bytes=2)
    reader = Stream(b"a" * (4 + extra))
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await run.snapshot(reader, kind=kind)
        assert run.events[0].resource in {Resource.FILE_BYTES, Resource.STREAM_BYTES}
    else:
        snapshot = await run.snapshot(reader, kind=kind)
        assert snapshot.size_bytes == 4
        assert await snapshot.read(offset=0, size=4) == b"aaaa"
    assert max(reader.sizes) <= 2
    assert reader.offset <= 5


@pytest.mark.anyio
async def test_declared_file_size_and_unknown_kind_fail_before_read() -> None:
    for kind, size in (("file", 5), ("url", None)):
        reader = Stream(b"secret-canary")
        run = session(max_file_bytes=4)
        with pytest.raises(SecurityPolicyError):
            await run.snapshot(reader, kind=kind, expected_size=size)
        assert reader.sizes == []
        assert "secret-canary" not in run.events[0].canonical_json()


@pytest.mark.anyio
@pytest.mark.parametrize("resource", ["max_records", "max_text_chars", "max_chunks"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_actual_text_parser_limits(resource: str, extra: int) -> None:
    content = (
        b"ab" + (b"c" if extra else b"")
        if resource == "max_text_chars"
        else b"x\n" * (2 + extra)
    )
    run = session(**{resource: 2})
    source = source_for(content, display_name="data.txt")
    _, context = contexts_for(source, content)
    context = replace(
        context, batch_options=replace(context.batch_options, batch_size=1)
    )

    async def collect() -> list[object]:
        async with run.parse(PlainTextParser(), source, context) as batches:
            return [batch async for batch in batches]

    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect()
        assert run.events and "data.txt" not in run.events[0].canonical_json()
    else:
        assert await collect()


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_csv_columns_before_hostile_field_accumulation(extra: int) -> None:
    content = b"a,b" + (b",secret-canary" if extra else b"") + b"\n"
    run = session(max_columns=2, read_chunk_bytes=1)
    source = source_for(content, display_name="data.csv")
    _, context = contexts_for(source, content)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(read_chunk_bytes=1),
        detection_options=DelimitedDetectionOptions(
            dialect_override=DelimitedDialect(delimiter=",")
        ),
    )

    async def collect() -> None:
        async with run.parse(parser, source, context) as batches:
            async for _ in batches:
                pass

    if extra:
        with pytest.raises(SecurityPolicyError):
            await collect()
        assert "secret-canary" not in run.events[0].canonical_json()
    else:
        await collect()


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_json_nesting_boundary(extra: int) -> None:
    # Existing parser считает root=1 и скаляр следующей глубиной.
    content = b"[" * (1 + extra) + b"0" + b"]" * (1 + extra)
    run = session(max_nesting_depth=2)
    source = source_for(content, display_name="data.json")
    _, context = contexts_for(source, content)

    async def collect() -> None:
        async with run.parse(JsonDocumentParser(), source, context) as batches:
            async for _ in batches:
                pass

    if extra:
        with pytest.raises(SecurityPolicyError):
            await collect()
    else:
        await collect()


def test_local_stricter_parser_limits_are_preserved() -> None:
    parser = PlainTextParser(limits=TextParserLimits(max_line_chars=2))
    bounded = session(max_text_chars=10).bounded_parser(parser)
    assert isinstance(bounded, PlainTextParser)
    assert bounded.limits.max_line_chars == 2
    assert parser.limits.read_chunk_bytes == TextParserLimits().read_chunk_bytes


@pytest.mark.anyio
async def test_missing_allowlist_or_trust_refuses_before_source_read() -> None:
    source = source_for(b"hello", display_name="source.txt")
    for policy in (SecurityPolicy(), SecurityPolicy(allowed_formats=("txt",))):
        run = SecuritySession(policy, run_id=UUID(int=1))
        _, context = contexts_for(source, b"hello")
        with pytest.raises(SecurityPolicyError):
            async with run.parse(PlainTextParser(), source, context):
                pytest.fail("Policy без authority допустила parser")
        assert run.events


@pytest.mark.anyio
async def test_cancellation_propagates_and_prevents_continuation() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingReader:
        async def read(self, size: int) -> bytes:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
            return b""

    run = session()
    task = asyncio.create_task(run.snapshot(WaitingReader(), kind="stream"))
    await started.wait()
    task.cancel("password=secret-canary")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert cancelled.is_set()
    assert error.value.args == ()
    assert run.events[0].outcome == "cancelled"
    assert "secret-canary" not in run.events[0].canonical_json()
    with pytest.raises(SecurityPolicyError):
        run.reserve(Resource.DB_QUERIES, 1)


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_json_object_columns_boundary(extra: int) -> None:
    content = b'{"a":0,"b":0' + (b',"c":"secret-canary"' if extra else b"") + b"}"
    run = session(max_columns=2, read_chunk_bytes=1)
    source = source_for(content, display_name="data.json")
    _, context = contexts_for(source, content)

    async def parse() -> None:
        async with run.parse(JsonDocumentParser(), source, context) as batches:
            async for _ in batches:
                pass

    if extra:
        with pytest.raises(SecurityPolicyError):
            await parse()
        assert "secret-canary" not in run.events[0].canonical_json()
    else:
        await parse()


@pytest.mark.anyio
@pytest.mark.parametrize("elapsed", [9, 10, 11])
async def test_parser_result_after_deadline_is_rejected(elapsed: int) -> None:
    clock = Clock()
    content = b"one\n"
    source = source_for(content, display_name="data.txt")

    class Reader:
        source_fingerprint = source.source_fingerprint
        expire = False

        async def read(self, *, offset: int, size: int) -> bytes:
            if self.expire:
                clock.seconds = elapsed / 1000
            return content[offset : offset + size]

    reader = Reader()
    _, context = contexts_for(source, content, reader=reader)
    run = SecuritySession(
        session(max_parser_time_ms=10).policy, run_id=UUID(int=1), monotonic=clock
    )
    async with run.parse(PlainTextParser(), source, context) as batches:
        reader.expire = True
        if elapsed < 10:
            assert [batch async for batch in batches]
        else:
            with pytest.raises(SecurityPolicyError, match="PROCESSING_TIMEOUT"):
                await anext(batches)
            assert run.events[0].resource is Resource.PARSER_TIME_MS


@pytest.mark.anyio
async def test_parser_cancellation_reaches_pending_reader() -> None:
    content = b"one\n"
    source = source_for(content, display_name="data.txt")
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class Reader:
        source_fingerprint = source.source_fingerprint
        block = False

        async def read(self, *, offset: int, size: int) -> bytes:
            if self.block:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()
            return content[offset : offset + size]

    reader = Reader()
    _, context = contexts_for(source, content, reader=reader)
    run = session()
    async with run.parse(PlainTextParser(), source, context) as batches:
        reader.block = True

        async def consume() -> None:
            await anext(batches)

        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel("secret-canary")
        with pytest.raises(asyncio.CancelledError) as error:
            await task
    assert cleaned.is_set() and error.value.args == ()
    assert run.events[0].outcome == "cancelled"


@pytest.mark.anyio
async def test_transport_over_return_is_rejected_before_buffering() -> None:
    class Reader:
        async def read(self, size: int) -> bytes:
            return b"secret-canary"

    run = session(read_chunk_bytes=1)
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED"):
        await run.snapshot(Reader(), kind="stream")
    assert "secret-canary" not in run.events[0].canonical_json()


@pytest.mark.anyio
async def test_session_does_not_accumulate_multiple_source_snapshots() -> None:
    run = session(max_stream_bytes=1)
    await run.snapshot(Stream(b"a"), kind="stream")
    second = Stream(b"b")
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED"):
        await run.snapshot(second, kind="stream")
    assert not second.sizes


@pytest.mark.anyio
async def test_adapter_exception_text_is_not_chained_into_security_error() -> None:
    async def fail() -> None:
        raise ValueError("password=secret-canary")

    run = session()
    with pytest.raises(SecurityPolicyError, match="SECURITY_OPERATION_FAILED") as error:
        await run.call(fail)
    assert error.value.__suppress_context__
    assert "secret-canary" not in "".join(traceback.format_exception(error.value))
    assert "secret-canary" not in run.events[0].canonical_json()
