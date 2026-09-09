from __future__ import annotations

import asyncio
import inspect
import traceback
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Never, cast, get_args, get_origin, get_type_hints

import pytest
from tests.contract_suites.parser import (
    ParserContractCase,
    assert_parser_contract,
)
from tests.fakes.parsers import (
    FakeParser,
    FakeSourceReader,
    valid_extracted_batches,
)

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedDatasetManifest,
    ExtractedLine,
    ExtractedSourceIndex,
    LineRangeLocation,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.ports import Parser
from structuraguard.ports.source import BatchOptions, ParseContext, ProbeContext

SOURCE_FINGERPRINT = "sha256:" + "a" * 64


class _ScriptedParser:
    adapter_id = "fake.parser"
    version = "1.0.0"
    priority = 0

    def __init__(
        self,
        probe_result: ProbeResult,
        iterators: tuple[AsyncIterator[ExtractedBatch], ...],
    ) -> None:
        self._probe_result = probe_result
        self._iterators = iter(iterators)

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del source, context
        return self._probe_result

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        return next(self._iterators)


class _SecondProbeBlockingParser(_ScriptedParser):
    def __init__(
        self,
        probe_result: ProbeResult,
        iterators: tuple[AsyncIterator[ExtractedBatch], ...],
    ) -> None:
        super().__init__(probe_result, iterators)
        self._probe_calls = 0
        self.second_probe_started = asyncio.Event()
        self.second_probe_release = asyncio.Event()

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        self._probe_calls += 1
        if self._probe_calls == 2:
            self.second_probe_started.set()
            await self.second_probe_release.wait()
        return await super().probe(source, context)


class _InvalidParseResultParser:
    adapter_id = "fake.parser"
    version = "1.0.0"
    priority = 0

    def __init__(
        self,
        probe_result: ProbeResult,
        output_factory: Callable[[], object],
    ) -> None:
        self._probe_result = probe_result
        self._output_factory = output_factory

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del source, context
        return self._probe_result

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        return cast(AsyncIterator[ExtractedBatch], self._output_factory())


class _BlockingCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self, batch: ExtractedBatch) -> None:
        self._batch = batch
        self._yielded = False
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> _BlockingCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return self._batch

    async def aclose(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


class _BlockedReadIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self, batch: ExtractedBatch) -> None:
        self._batch = batch
        self._yielded = False
        self.read_started = asyncio.Event()
        self.read_release = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> _BlockedReadIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        if self._yielded:
            raise StopAsyncIteration
        self.read_started.set()
        await self.read_release.wait()
        self._yielded = True
        return self._batch

    async def aclose(self) -> None:
        self.closed = True


class _TrackingIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self, batches: tuple[ExtractedBatch, ...]) -> None:
        self._batches = iter(batches)
        self.closed = False

    def __aiter__(self) -> _TrackingIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        if self.closed:
            raise StopAsyncIteration
        try:
            return next(self._batches)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.closed = True


class _FailOnceCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self, items: tuple[object, ...] = ()) -> None:
        self._items = iter(items)
        self.close_attempts = 0
        self.closed = False

    def __aiter__(self) -> _FailOnceCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        try:
            return cast(ExtractedBatch, next(self._items))
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise RuntimeError("password=DO_NOT_LEAK_CLOSE_FAILURE")
        self.closed = True


class _CancelOnceCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(
        self,
        items: tuple[ExtractedBatch, ...] = (),
        *,
        cancellation_marker: str | None = None,
    ) -> None:
        self._items = iter(items)
        self._cancellation_marker = cancellation_marker
        self.close_attempts = 0
        self.closed = False

    def __aiter__(self) -> _CancelOnceCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            marker = self._cancellation_marker
            if marker is None:
                raise asyncio.CancelledError
            try:
                raise RuntimeError(marker)
            except RuntimeError as error:
                cancellation = asyncio.CancelledError(marker)
                cancellation.add_note(marker)
                raise cancellation from error
        self.closed = True


class _HostileBatchValue:
    def __repr__(self) -> str:
        return "password=DO_NOT_LEAK_BATCH_SCHEMA"


class _LengthOnlyHostileLines:
    def __len__(self) -> int:
        return 1_000_000

    def __iter__(self) -> Never:
        raise AssertionError("overflowed nested lines не должны обходиться")


def _cancellation_with_context(marker: str) -> asyncio.CancelledError:
    try:
        raise RuntimeError(marker)
    except RuntimeError as error:
        cancellation = asyncio.CancelledError(marker)
        cancellation.add_note(marker)
        try:
            raise cancellation from error
        except asyncio.CancelledError as raised:
            return raised


def _hostile_base_exception_group(marker: str) -> BaseExceptionGroup:
    failure = RuntimeError(marker)
    failure.add_note(marker)
    return BaseExceptionGroup(
        marker,
        (_cancellation_with_context(marker), failure),
    )


def _assert_sanitized_parser_group(
    group: BaseExceptionGroup,
    *,
    marker: str,
    reason: str,
) -> None:
    cancellations = tuple(
        error for error in group.exceptions if isinstance(error, asyncio.CancelledError)
    )
    parser_errors = tuple(
        error for error in group.exceptions if isinstance(error, ParserError)
    )
    assert group.__context__ is None
    assert group.__cause__ is None
    assert getattr(group, "__notes__", ()) == ()
    assert len(cancellations) == 1
    assert cancellations[0].args == ()
    assert cancellations[0].__context__ is None
    assert cancellations[0].__cause__ is None
    assert getattr(cancellations[0], "__notes__", ()) == ()
    assert len(parser_errors) == 1
    assert parser_errors[0].error_code == "PARSER_OUTPUT_INVALID"
    assert parser_errors[0].details["reason"] == reason
    assert marker not in "".join(traceback.format_exception(group))


def _security_error_with_context(marker: str, message: str) -> SecurityPolicyError:
    try:
        raise RuntimeError(marker)
    except RuntimeError:
        try:
            security_error = SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED",
                message=message,
                details={"resource": "bytes"},
            )
            security_error.add_note(marker)
            raise security_error
        except SecurityPolicyError as error:
            return error


def _source() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="source-1",
        display_name="sample.fake",
        media_type="application/x-fake",
        size_bytes=32,
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _contexts() -> tuple[ProbeContext, ParseContext]:
    reader = FakeSourceReader(SOURCE_FINGERPRINT, content=b"fake source")
    return (
        ProbeContext(
            reader=reader,
            source_fingerprint=SOURCE_FINGERPRINT,
            max_probe_bytes=64,
        ),
        ParseContext(
            reader=reader,
            source_fingerprint=SOURCE_FINGERPRINT,
            max_bytes=128,
            max_records=16,
            max_nesting_depth=4,
        ),
    )


def _probe(source: SourceArtifact) -> ProbeResult:
    return ProbeResult(
        source=source.ref,
        adapter_id="fake.parser",
        adapter_version="1.0.0",
        supported=True,
        confidence=Decimal("1"),
        detected_media_type="application/x-fake",
        format_id="fake",
        signals=(
            ProbeSignal(
                kind=ProbeSignalKind.SIGNATURE,
                outcome=ProbeSignalOutcome.MATCH,
            ),
        ),
    )


def _two_line_batch(source: SourceArtifact) -> ExtractedBatch:
    return ExtractedBatch(
        extraction_id="extraction-1",
        batch_index=0,
        source=source.ref,
        parser_id="fake.parser",
        parser_version="1.0.0",
        batch_fingerprint="sha256:" + "b" * 64,
        lines=tuple(
            ExtractedLine(
                line_id=f"line-{index}",
                line_number=index,
                text="value",
                location=LineRangeLocation(
                    source=source.ref,
                    line_start=index,
                    line_end=index,
                ),
            )
            for index in (1, 2)
        ),
    )


@pytest.mark.anyio
async def test_reusable_contract_suite_accepts_physical_fake_parser() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    parser = FakeParser(
        probe_result=_probe(source),
        batches=valid_extracted_batches(
            source,
            parser_id="fake.parser",
            parser_version="1.0.0",
            batch_count=2,
        ),
    )

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=parser,
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    assert len(batches) == 2
    assert all(type(batch) is ExtractedBatch for batch in batches)
    assert parser.probe_call_count == 1
    assert parser.parse_call_count == 1


@pytest.mark.anyio
async def test_contract_suite_rejects_non_physical_parser_output() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    parser = FakeParser(
        probe_result=_probe(source),
        batches=({"entity": "customer", "target_table": "customers"},),
    )

    with pytest.raises(ParserError) as raised:
        await assert_parser_contract(
            ParserContractCase(
                parser=parser,
                source=source,
                probe_context=probe_context,
                parse_context=parse_context,
            )
        )

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"


@pytest.mark.anyio
async def test_contract_suite_rejects_supported_probe_without_m03_evidence() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    parser = FakeParser(
        probe_result=ProbeResult(
            source=source.ref,
            adapter_id="fake.parser",
            adapter_version="1.0.0",
            supported=True,
            confidence=Decimal("0.90"),
            detected_media_type="application/x-fake",
        ),
        batches=valid_extracted_batches(source),
    )

    with pytest.raises(ParserError) as raised:
        await assert_parser_contract(
            ParserContractCase(
                parser=parser,
                source=source,
                probe_context=probe_context,
                parse_context=parse_context,
            )
        )

    assert raised.value.error_code == "PARSER_PROBE_INVALID"


def test_parser_port_exposes_only_physical_input_and_output_boundary() -> None:
    probe_signature = inspect.signature(Parser.probe)
    parse_signature = inspect.signature(Parser.parse)
    parse_return = get_type_hints(Parser.parse)["return"]

    assert tuple(probe_signature.parameters) == ("self", "source", "context")
    assert tuple(parse_signature.parameters) == ("self", "source", "context")
    assert get_origin(parse_return) is AsyncIterator
    assert get_args(parse_return) == (ExtractedBatch,)
    assert {
        "entity_type",
        "semantic_type",
        "mapping_plan",
        "target_table",
        "database",
        "llm",
    }.isdisjoint(ExtractedBatch.model_fields)


def test_fake_parser_satisfies_runtime_parser_protocol() -> None:
    assert isinstance(FakeParser(), Parser)


@pytest.mark.anyio
async def test_registry_stream_validates_physical_batches_until_exhaustion() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    expected = valid_extracted_batches(source, batch_count=2)
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=expected))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        actual = tuple([batch async for batch in stream])

        assert actual == expected
        assert stream.completed is True


@pytest.mark.anyio
async def test_registry_rejects_business_object_before_yield() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    parser = FakeParser(
        probe_result=_probe(source),
        batches=({"entity": "customer", "target_table": "customers"},),
    )
    registry = ParserRegistry()
    registry.register(parser)

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_type"


@pytest.mark.anyio
@pytest.mark.parametrize("output_factory", (lambda: [], lambda: iter(())))
async def test_registry_rejects_non_async_parse_result(
    output_factory: Callable[[], object],
) -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(_InvalidParseResultParser(_probe(source), output_factory))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(ParserError) as raised:
            selected.parse(source, parse_context)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "parse_not_async_iterator"


@pytest.mark.anyio
async def test_registry_rejects_coroutine_instead_of_parse_iterator() -> None:
    source = _source()
    probe_context, parse_context = _contexts()

    async def coroutine_result() -> ExtractedBatch:
        return valid_extracted_batches(source)[0]

    registry = ParserRegistry()
    registry.register(
        _InvalidParseResultParser(
            _probe(source),
            coroutine_result,
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(ParserError) as raised:
            selected.parse(source, parse_context)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "parse_not_async_iterator"


@pytest.mark.anyio
async def test_registry_wraps_unexpected_parse_call_error() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_call_error=RuntimeError("password=DO_NOT_LEAK_PARSE_CALL"),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(ParserError) as raised:
            selected.parse(source, parse_context)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "parse_call_failed"
    assert raised.value.cause == "RuntimeError"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_PARSE_CALL" not in " ".join(
        (
            repr(raised.value),
            repr(raised.value.__context__),
            "".join(traceback.format_exception(raised.value)),
        )
    )


@pytest.mark.anyio
async def test_parse_call_cancellation_is_sanitized_without_creating_stream() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    marker = "password=DO_NOT_LEAK_PARSE_CANCELLATION"
    parser = FakeParser(
        probe_result=_probe(source),
        parse_call_error=_cancellation_with_context(marker),
    )
    registry = ParserRegistry()
    registry.register(parser)

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(asyncio.CancelledError) as raised:
            selected.parse(source, parse_context)

    assert parser.parse_call_count == 1
    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.args == ()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in "".join(traceback.format_exception(raised.value))
    registry.register(FakeParser(adapter_id="fake.second"))


@pytest.mark.anyio
async def test_registry_wraps_iteration_error_without_raw_exception_chain() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_error=RuntimeError("password=DO_NOT_LEAK_PARSE_ITERATION"),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "parse_iteration_failed"
    assert raised.value.cause == "RuntimeError"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_PARSE_ITERATION" not in " ".join(
        (
            repr(raised.value),
            repr(raised.value.__context__),
            "".join(traceback.format_exception(raised.value)),
        )
    )


@pytest.mark.anyio
async def test_registry_rejects_stream_without_terminal_manifest() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    non_terminal = terminal.model_copy(update={"is_last": False, "manifest": None})
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(non_terminal,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == non_terminal
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "missing_terminal_manifest"


@pytest.mark.anyio
async def test_registry_rejects_output_after_terminal_batch() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    registry = ParserRegistry()
    registry.register(
        FakeParser(probe_result=_probe(source), batches=(terminal, terminal))
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == terminal
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_after_terminal"


@pytest.mark.anyio
async def test_selected_handle_fails_after_session_exit() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    with pytest.raises(ParserError) as raised:
        selected.parse(source, parse_context)

    assert raised.value.error_code == "PARSER_SESSION_CLOSED"


@pytest.mark.anyio
async def test_registry_enforces_record_limit_with_security_error() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    limited_context = ParseContext(
        reader=parse_context.reader,
        source_fingerprint=parse_context.source_fingerprint,
        max_bytes=parse_context.max_bytes,
        max_records=1,
        max_nesting_depth=parse_context.max_nesting_depth,
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            batches=valid_extracted_batches(source, batch_count=2),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, limited_context)
        await anext(stream)
        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_registry_uses_builtin_resource_name_for_batch_limit() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    limited_context = ParseContext(
        reader=parse_context.reader,
        source_fingerprint=parse_context.source_fingerprint,
        max_bytes=parse_context.max_bytes,
        max_records=parse_context.max_records,
        max_nesting_depth=parse_context.max_nesting_depth,
        batch_options=BatchOptions(batch_size=1, max_batches=1),
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            batches=valid_extracted_batches(source, batch_count=2),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, limited_context)
        await anext(stream)
        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "batch_count"


@pytest.mark.anyio
async def test_record_limit_rejects_batch_before_deep_copy_or_ref_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    limited_context = ParseContext(
        reader=parse_context.reader,
        source_fingerprint=parse_context.source_fingerprint,
        max_bytes=parse_context.max_bytes,
        max_records=1,
        max_nesting_depth=parse_context.max_nesting_depth,
    )
    oversized = _two_line_batch(source)
    iterator = _TrackingIterator((oversized,))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))
    expensive_calls: list[str] = []

    def fail_if_called(*args: object, **kwargs: object) -> None:
        del args, kwargs
        expensive_calls.append("called")
        raise AssertionError("oversized batch reached an expensive operation")

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, limited_context)
        monkeypatch.setattr(ExtractedBatch, "model_dump", fail_if_called)
        monkeypatch.setattr(ExtractedBatch, "to_summary", fail_if_called)
        monkeypatch.setattr(ExtractedBatch, "physical_refs", fail_if_called)

        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "records"
    assert expensive_calls == []
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_physical_limit_rejects_nested_collection_before_traversal() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    limited_context = ParseContext(
        reader=parse_context.reader,
        source_fingerprint=parse_context.source_fingerprint,
        max_bytes=parse_context.max_bytes,
        max_records=parse_context.max_records,
        max_nesting_depth=parse_context.max_nesting_depth,
        max_physical_objects=1,
    )
    block = ExtractedBlock(
        block_id="block-1",
        kind=ExtractedBlockKind.LINE,
        order=0,
        location=LineRangeLocation(
            source=source.ref,
            line_start=1,
            line_end=1,
        ),
        text="raw",
    ).model_copy(update={"lines": _LengthOnlyHostileLines()})
    forged = valid_extracted_batches(source)[0].model_copy(
        update={"lines": (), "blocks": (block,)}
    )
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, limited_context)
        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details["resource"] == "physical_objects"


@pytest.mark.anyio
async def test_output_failure_preserves_failed_cleanup_for_retry() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _FailOnceCloseIterator(({"entity": "customer"},))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)

        with pytest.raises(ExceptionGroup) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert (iterator.close_attempts, iterator.closed) == (1, False)
        await stream.aclose()

    primary, cleanup = raised.value.exceptions
    assert isinstance(primary, ParserError)
    assert primary.error_code == "PARSER_OUTPUT_INVALID"
    assert primary.details["reason"] == "batch_type"
    assert isinstance(cleanup, ParserError)
    assert cleanup.error_code == "PARSER_OUTPUT_INVALID"
    assert cleanup.details["reason"] == "iterator_close_failed"
    assert cleanup.cause == "RuntimeError"
    assert cleanup.__context__ is None
    assert cleanup.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_CLOSE_FAILURE" not in "".join(
        traceback.format_exception(raised.value)
    )
    assert (iterator.close_attempts, iterator.closed, stream.completed) == (
        2,
        True,
        False,
    )


@pytest.mark.anyio
async def test_record_limit_preserves_failed_cleanup_for_retry() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    limited_context = ParseContext(
        reader=parse_context.reader,
        source_fingerprint=parse_context.source_fingerprint,
        max_bytes=parse_context.max_bytes,
        max_records=1,
        max_nesting_depth=parse_context.max_nesting_depth,
    )
    iterator = _FailOnceCloseIterator((_two_line_batch(source),))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, limited_context)

        with pytest.raises(ExceptionGroup) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert (iterator.close_attempts, iterator.closed) == (1, False)
        await stream.aclose()

    primary, cleanup = raised.value.exceptions
    assert isinstance(primary, SecurityPolicyError)
    assert primary.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert isinstance(cleanup, ParserError)
    assert cleanup.error_code == "PARSER_OUTPUT_INVALID"
    assert cleanup.details["reason"] == "iterator_close_failed"
    assert cleanup.cause == "RuntimeError"
    assert cleanup.__context__ is None
    assert cleanup.__cause__ is None
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_CLOSE_FAILURE" not in "".join(
        traceback.format_exception(raised.value)
    )
    assert (iterator.close_attempts, iterator.closed, stream.completed) == (
        2,
        True,
        False,
    )


@pytest.mark.anyio
async def test_parser_iterator_security_error_is_preserved() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    expected = _security_error_with_context(
        "password=DO_NOT_LEAK_ITERATOR_SECURITY_CONTEXT",
        "Parser byte budget исчерпан.",
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_error=expected,
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)

    assert raised.value is expected
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert "DO_NOT_LEAK_ITERATOR_SECURITY_CONTEXT" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.anyio
async def test_parser_call_security_error_is_preserved() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    expected = _security_error_with_context(
        "password=DO_NOT_LEAK_CALL_SECURITY_CONTEXT",
        "Parser отклонён до создания iterator.",
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_call_error=expected,
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(SecurityPolicyError) as raised:
            selected.parse(source, parse_context)

    assert raised.value is expected
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert "DO_NOT_LEAK_CALL_SECURITY_CONTEXT" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.anyio
async def test_parser_call_base_exception_group_is_sanitized() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    marker = "password=DO_NOT_LEAK_PARSE_CALL_GROUP"
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_call_error=_hostile_base_exception_group(marker),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(BaseExceptionGroup) as raised:
            selected.parse(source, parse_context)

    _assert_sanitized_parser_group(
        raised.value,
        marker=marker,
        reason="parse_call_failed",
    )


@pytest.mark.anyio
async def test_parser_iteration_base_exception_group_is_sanitized_and_quarantined() -> (
    None
):
    source = _source()
    probe_context, parse_context = _contexts()
    marker = "password=DO_NOT_LEAK_PARSE_ITERATION_GROUP"
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_result=_probe(source),
            parse_error=_hostile_base_exception_group(marker),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(BaseExceptionGroup) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    _assert_sanitized_parser_group(
        raised.value,
        marker=marker,
        reason="parse_iteration_failed",
    )
    assert stream.completed is False


@pytest.mark.anyio
async def test_registry_rejects_manifest_ref_missing_from_previous_batch() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    extraction_id = "extraction-1"
    first = ExtractedBatch(
        extraction_id=extraction_id,
        batch_index=0,
        source=source.ref,
        parser_id="fake.parser",
        parser_version="1.0.0",
        batch_fingerprint="sha256:" + "b" * 64,
        lines=(
            ExtractedLine(
                line_id="line-1",
                line_number=1,
                text="value",
                location=LineRangeLocation(
                    source=source.ref,
                    line_start=1,
                    line_end=1,
                ),
            ),
        ),
    )
    terminal_seed = ExtractedBatch(
        extraction_id=extraction_id,
        batch_index=1,
        source=source.ref,
        parser_id="fake.parser",
        parser_version="1.0.0",
        batch_fingerprint="sha256:" + "c" * 64,
    )
    manifest = ExtractedDatasetManifest(
        source=source.ref,
        extraction_id=extraction_id,
        parser_id="fake.parser",
        parser_version="1.0.0",
        batches=(first.to_summary(), terminal_seed.to_summary()),
        extraction_fingerprint="sha256:" + "d" * 64,
        source_index=ExtractedSourceIndex(
            refs=(
                PhysicalSourceRef(
                    extraction_id=extraction_id,
                    batch_index=0,
                    kind=PhysicalObjectKind.LINE,
                    local_id="line-2",
                ),
            )
        ),
    )
    terminal = ExtractedBatch(
        extraction_id=extraction_id,
        batch_index=1,
        source=source.ref,
        parser_id="fake.parser",
        parser_version="1.0.0",
        batch_fingerprint=terminal_seed.batch_fingerprint,
        is_last=True,
        manifest=manifest,
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(probe_result=_probe(source), batches=(first, terminal))
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == first
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "manifest_source_index"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ("duplicate_index", "batch_sequence"),
        ("gapped_index", "batch_sequence"),
        ("source", "source_identity"),
        ("parser_id", "parser_identity"),
        ("parser_version", "parser_identity"),
        ("extraction_id", "extraction_identity"),
        ("fingerprint", "batch_fingerprint_duplicate"),
    ),
)
async def test_registry_rejects_forged_runtime_sequence_and_lineage(
    mutation: str,
    reason: str,
) -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    batches = list(valid_extracted_batches(source, batch_count=3))
    second = batches[1]
    if mutation == "duplicate_index":
        second = second.model_copy(update={"batch_index": 0})
    elif mutation == "gapped_index":
        second = second.model_copy(update={"batch_index": 2})
    elif mutation == "source":
        foreign = source.model_copy(update={"artifact_id": "source-foreign"})
        second = second.model_copy(update={"source": foreign.ref})
    elif mutation == "parser_id":
        second = second.model_copy(update={"parser_id": "other.parser"})
    elif mutation == "parser_version":
        second = second.model_copy(update={"parser_version": "2.0.0"})
    elif mutation == "extraction_id":
        second = second.model_copy(update={"extraction_id": "extraction-foreign"})
    else:
        second = second.model_copy(
            update={"batch_fingerprint": batches[0].batch_fingerprint}
        )
    batches[1] = second
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=tuple(batches)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == batches[0]
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == reason


@pytest.mark.anyio
async def test_session_closes_early_abandoned_stream() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _TrackingIterator(valid_extracted_batches(source, batch_count=2))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert (
            await anext(stream)
            == valid_extracted_batches(
                source,
                batch_count=2,
            )[0]
        )

    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_session_body_exception_closes_early_abandoned_stream() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _TrackingIterator(valid_extracted_batches(source, batch_count=2))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    with pytest.raises(RuntimeError, match="consumer failed"):
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            await anext(stream)
            raise RuntimeError("consumer failed")

    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_session_rejects_new_stream_before_awaiting_existing_cleanup() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    first_iterator = _BlockingCloseIterator(terminal)
    second_iterator = _BlockingCloseIterator(terminal)
    registry = ParserRegistry()
    registry.register(
        _ScriptedParser(_probe(source), (first_iterator, second_iterator))
    )
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    selected.parse(source, parse_context)

    exit_task = asyncio.create_task(session.__aexit__(None, None, None))
    await first_iterator.close_started.wait()
    with pytest.raises(ParserError) as raised:
        selected.parse(source, parse_context)
    first_iterator.close_release.set()
    await exit_task

    assert raised.value.error_code == "PARSER_SESSION_CLOSED"
    assert first_iterator.closed is True
    assert second_iterator.close_started.is_set() is False


@pytest.mark.anyio
async def test_in_flight_read_cannot_yield_after_session_exit_starts() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _BlockedReadIterator(valid_extracted_batches(source)[0])
    parser = _SecondProbeBlockingParser(_probe(source), (iterator,))
    registry = ParserRegistry()
    registry.register(parser)
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    stream = selected.parse(source, parse_context)
    read_task = asyncio.create_task(anext(stream))
    await iterator.read_started.wait()
    selection_task = asyncio.create_task(session.select(source, probe_context))
    await parser.second_probe_started.wait()

    exit_started = asyncio.Event()

    async def exit_session() -> None:
        exit_started.set()
        await session.__aexit__(None, None, None)

    exit_task = asyncio.create_task(exit_session())
    await exit_started.wait()
    with pytest.raises(ParserError) as raised:
        selected.parse(source, parse_context)

    selection_error: ParserError | None = None
    try:
        iterator.read_release.set()
        with pytest.raises(StopAsyncIteration):
            await read_task
    finally:
        parser.second_probe_release.set()
        try:
            await selection_task
        except ParserError as error:
            selection_error = error
        await exit_task
    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))

    assert raised.value.error_code == "PARSER_SESSION_CLOSED"
    assert selection_error is not None
    assert selection_error.error_code == "PARSER_SESSION_CLOSED"
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_overlapping_anext_fails_without_closing_first_read() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    iterator = _BlockedReadIterator(terminal)
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        first_read = asyncio.create_task(anext(stream))
        await iterator.read_started.wait()
        with pytest.raises(ParserError) as raised:
            await anext(stream)
        iterator.read_release.set()
        assert await first_read == terminal
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "concurrent_iteration"
    assert iterator.closed is True
    assert stream.completed is True


@pytest.mark.anyio
async def test_cancelled_anext_quarantines_stream_until_session_cleanup() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    iterator = _BlockedReadIterator(terminal)
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        read_task = asyncio.create_task(anext(stream))
        await iterator.read_started.wait()
        marker = "password=DO_NOT_LEAK_READ_CANCELLATION"
        read_task.cancel(marker)

        with pytest.raises(asyncio.CancelledError) as raised:
            await read_task
        iterator.read_release.set()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

        assert iterator.closed is False
        assert stream.completed is False
        assert type(raised.value) is asyncio.CancelledError
        assert raised.value.args == ()
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert getattr(raised.value, "__notes__", ()) == ()
        assert marker not in "".join(traceback.format_exception(raised.value))

    assert iterator.closed is True


@pytest.mark.anyio
async def test_cancelled_close_while_waiting_for_read_lock_is_sanitized() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    iterator = _BlockedReadIterator(terminal)
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        read_task = asyncio.create_task(anext(stream))
        await iterator.read_started.wait()
        close_task = asyncio.create_task(stream.aclose())
        await asyncio.sleep(0)
        marker = "password=DO_NOT_LEAK_CLOSE_LOCK_CANCELLATION"
        close_task.cancel(marker)

        with pytest.raises(asyncio.CancelledError) as raised:
            await close_task
        assert type(raised.value) is asyncio.CancelledError
        assert raised.value.args == ()
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert getattr(raised.value, "__notes__", ()) == ()
        assert marker not in "".join(traceback.format_exception(raised.value))

        iterator.read_release.set()
        with pytest.raises(StopAsyncIteration):
            await read_task

    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_exhaustion_close_failure_requires_explicit_retry() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    iterator = _FailOnceCloseIterator((terminal,))
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == terminal
        with pytest.raises(ParserError) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert (iterator.close_attempts, iterator.closed, stream.completed) == (
            1,
            False,
            False,
        )
        await stream.aclose()

    assert raised.value.details["reason"] == "iterator_close_failed"
    assert raised.value.retryable is True
    assert (iterator.close_attempts, iterator.closed) == (2, True)


@pytest.mark.anyio
async def test_exhaustion_close_cancellation_requires_explicit_retry() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    marker = "password=DO_NOT_LEAK_EXHAUSTION_CANCELLATION"
    iterator = _CancelOnceCloseIterator(
        (terminal,),
        cancellation_marker=marker,
    )
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == terminal
        with pytest.raises(asyncio.CancelledError) as raised:
            await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert (iterator.close_attempts, iterator.closed, stream.completed) == (
            1,
            False,
            False,
        )
        await stream.aclose()

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.args == ()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in "".join(traceback.format_exception(raised.value))
    assert (iterator.close_attempts, iterator.closed) == (2, True)


@pytest.mark.anyio
async def test_cancelled_session_exit_finishes_cleanup_before_propagating() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _BlockingCloseIterator(valid_extracted_batches(source)[0])
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    selected.parse(source, parse_context)

    exit_task = asyncio.create_task(session.__aexit__(None, None, None))
    await iterator.close_started.wait()
    exit_task.cancel()
    iterator.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await exit_task
    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))

    assert iterator.closed is True


@pytest.mark.anyio
async def test_failed_close_is_visible_quarantined_and_retryable() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    iterator = _FailOnceCloseIterator()
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (iterator,)))
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    stream = selected.parse(source, parse_context)

    with pytest.raises(ParserError) as close_failure:
        await session.__aexit__(None, None, None)
    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()

    assert close_failure.value.error_code == "PARSER_OUTPUT_INVALID"
    assert close_failure.value.details["reason"] == "iterator_close_failed"
    assert close_failure.value.retryable is True
    assert close_failure.value.__context__ is None
    assert close_failure.value.__cause__ is None
    assert "DO_NOT_LEAK_CLOSE_FAILURE" not in " ".join(
        (
            repr(close_failure.value),
            repr(close_failure.value.__context__),
            "".join(traceback.format_exception(close_failure.value)),
        )
    )
    assert iterator.close_attempts == 2
    assert iterator.closed is True
    assert stream.completed is False


@pytest.mark.anyio
async def test_cancelled_close_does_not_skip_sibling_cleanup() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    cancelled = _CancelOnceCloseIterator()
    sibling = _BlockedReadIterator(valid_extracted_batches(source)[0])
    registry = ParserRegistry()
    registry.register(_ScriptedParser(_probe(source), (cancelled, sibling)))
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    cancelled_stream = selected.parse(source, parse_context)
    selected.parse(source, parse_context)

    with pytest.raises(asyncio.CancelledError):
        await session.__aexit__(None, None, None)
    registry.register(FakeParser(adapter_id="fake.second", version="1.0.0", priority=0))
    await cancelled_stream.aclose()

    assert sibling.closed is True
    assert cancelled.close_attempts == 2
    assert cancelled.closed is True


@pytest.mark.anyio
async def test_completed_is_read_only_and_requires_successful_exhaustion() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    terminal = valid_extracted_batches(source)[0]
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(terminal,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(AttributeError):
            stream.completed = True  # type: ignore[misc]
        assert await anext(stream) == terminal
        assert stream.completed is False
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    assert stream.completed is True


@pytest.mark.anyio
async def test_forged_nested_provenance_is_revalidated_before_yield() -> None:
    source = _source()
    foreign_source = SourceArtifact(
        artifact_id="source-2",
        display_name="foreign.fake",
        media_type="application/x-fake",
        size_bytes=1,
        source_fingerprint="sha256:" + "d" * 64,
    )
    terminal = valid_extracted_batches(source)[0]
    forged = terminal.model_copy(
        update={
            "lines": (
                ExtractedLine(
                    line_id="line-1",
                    line_number=1,
                    text="untrusted",
                    location=LineRangeLocation(
                        source=foreign_source.ref,
                        line_start=1,
                        line_end=1,
                    ),
                ),
            )
        }
    )
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_schema"


@pytest.mark.anyio
async def test_forged_nested_object_returns_typed_error_not_attribute_error() -> None:
    source = _source()
    terminal = valid_extracted_batches(source)[0]
    forged = terminal.model_copy(update={"lines": (_HostileBatchValue(),)})
    probe_context, parse_context = _contexts()
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_schema"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_BATCH_SCHEMA" not in " ".join(
        (
            repr(raised.value),
            repr(raised.value.__context__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
