from __future__ import annotations

import asyncio
import threading
import traceback
from collections.abc import AsyncIterator, Awaitable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import cast, get_type_hints

import pytest
from tests.fakes.parsers import FakeSourceReader

import structuraguard.parsers.registry as registry_module
from structuraguard.contracts.plugins import (
    ParserDiscoveryPolicy,
    ParserPluginDescriptor,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import (
    ParserRegistry,
    ParserRegistrySession,
    ParserRegistrySnapshot,
)
from structuraguard.parsers.discovery import ParserPluginDiscoveryReport
from structuraguard.ports.source import ParseContext, ProbeContext


class _ParserA:
    adapter_id = "parser.a"
    version = "1.0.0"
    priority = 10

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        raise NotImplementedError

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        return self._empty()

    async def _empty(self) -> AsyncIterator[ExtractedBatch]:
        batches: tuple[ExtractedBatch, ...] = ()
        for batch in batches:
            yield batch


class _ParserB(_ParserA):
    adapter_id = "parser.b"
    priority = 20


class _ObservableParser:
    version = "1.0.0"
    priority = 20

    def __init__(self) -> None:
        self.identity_reads = 0

    @property
    def adapter_id(self) -> str:
        self.identity_reads += 1
        return "parser.observable"

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        raise NotImplementedError

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        return self._empty()

    async def _empty(self) -> AsyncIterator[ExtractedBatch]:
        batches: tuple[ExtractedBatch, ...] = ()
        for batch in batches:
            yield batch


class _ExplodingIdentityParser:
    version = "1.0.0"
    priority = 0

    @property
    def adapter_id(self) -> str:
        raise RuntimeError("DO_NOT_LEAK_IDENTITY_GETTER")

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        raise NotImplementedError

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        return self._empty()

    async def _empty(self) -> AsyncIterator[ExtractedBatch]:
        batches: tuple[ExtractedBatch, ...] = ()
        for batch in batches:
            yield batch


class _ExplodingProtocolMeta(type):
    def __instancecheck__(cls, instance: object) -> bool:
        del cls, instance
        raise RuntimeError("password=DO_NOT_LEAK_PROTOCOL_INSPECTION")


class _ExplodingProtocol(metaclass=_ExplodingProtocolMeta):
    pass


_SOURCE_FINGERPRINT = "sha256:" + "a" * 64


class _FailOnceCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self, marker: str, *, blocked: bool = False) -> None:
        self._marker = marker
        self._blocked = blocked
        self.close_attempts = 0
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False

    def __aiter__(self) -> _FailOnceCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            self.close_started.set()
            if self._blocked:
                await self.close_release.wait()
            raise RuntimeError(self._marker)
        self.closed = True


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


class _GroupedCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self) -> None:
        self.close_attempts = 0
        self.closed = False

    def __aiter__(self) -> _GroupedCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise BaseExceptionGroup(
                "password=DO_NOT_LEAK_GROUP_MESSAGE",
                (
                    _cancellation_with_context(
                        "password=DO_NOT_LEAK_GROUP_CANCELLATION"
                    ),
                    RuntimeError("password=DO_NOT_LEAK_GROUP_RUNTIME"),
                    ExceptionGroup(
                        "password=DO_NOT_LEAK_NESTED_GROUP_MESSAGE",
                        (ValueError("password=DO_NOT_LEAK_GROUP_VALUE"),),
                    ),
                ),
            )
        self.closed = True


class _SuccessfulCloseIterator(AsyncIterator[ExtractedBatch]):
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self) -> _SuccessfulCloseIterator:
        return self

    async def __anext__(self) -> ExtractedBatch:
        raise StopAsyncIteration

    async def aclose(self) -> None:
        self.closed = True


class _CloseScriptedParser(_ParserA):
    priority = 0

    def __init__(
        self,
        iterators: tuple[AsyncIterator[ExtractedBatch], ...],
    ) -> None:
        self._iterators = iter(iterators)

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        del context
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
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

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        return next(self._iterators)


def _runtime_source_contexts() -> tuple[SourceArtifact, ProbeContext, ParseContext]:
    source = SourceArtifact(
        artifact_id="source-1",
        display_name="sample.fake",
        media_type="application/x-fake",
        size_bytes=32,
        source_fingerprint=_SOURCE_FINGERPRINT,
    )
    reader = FakeSourceReader(_SOURCE_FINGERPRINT, content=b"fake source")
    return (
        source,
        ProbeContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_probe_bytes=64,
        ),
        ParseContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_bytes=128,
            max_records=16,
            max_nesting_depth=4,
        ),
    )


def test_public_registry_type_hints_resolve_at_runtime() -> None:
    snapshot_hints = get_type_hints(ParserRegistrySnapshot)
    discovery_hints = get_type_hints(ParserRegistry.discover_plugins)
    select_hints = get_type_hints(ParserRegistrySession.select)

    assert snapshot_hints["plugins"] == tuple[ParserPluginDescriptor, ...]
    assert discovery_hints["policy"] is ParserDiscoveryPolicy
    assert discovery_hints["return"] is ParserPluginDiscoveryReport
    assert select_hints["source"] is SourceArtifact


def test_registries_are_instance_local_and_snapshots_are_canonical() -> None:
    first = ParserRegistry()
    second = ParserRegistry()

    first.register_many((_ParserB(), _ParserA()))
    reverse = ParserRegistry()
    reverse.register_many((_ParserA(), _ParserB()))

    assert tuple(item.adapter_id for item in first.snapshot().parsers) == (
        "parser.a",
        "parser.b",
    )
    assert first.snapshot().fingerprint == reverse.snapshot().fingerprint
    assert second.snapshot().parsers == ()


def test_snapshot_is_frozen_and_detached_from_parser_identity_mutation() -> None:
    registry = ParserRegistry()
    parser = _ParserA()
    registry.register(parser)
    snapshot = registry.snapshot()

    parser.adapter_id = "parser.changed"
    parser.version = "9.0.0"
    parser.priority = 999

    assert registry.snapshot() == snapshot
    with pytest.raises(FrozenInstanceError):
        snapshot.fingerprint = "sha256:" + "0" * 64  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.parsers[0].priority = 999  # type: ignore[misc]


def test_registered_parser_class_target_is_detached_from_later_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_module = _ParserA.__module__
    original_qualname = _ParserA.__qualname__
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module=original_module,
        attribute=original_qualname,
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    registry = ParserRegistry()
    registry.register(_ParserA())
    before = registry.snapshot()
    monkeypatch.setattr(_ParserA, "__module__", ["unhashable", "metadata"])

    report = registry.discover_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    )

    assert report.descriptors == ()
    assert len(report.failures) == 1
    assert report.failures[0].error.error_code == "PARSER_DUPLICATE_REGISTRATION"
    assert report.failures[0].error.details["duplicate_kind"] == "parser_class"
    assert registry.snapshot() == before


@pytest.mark.parametrize(
    "malformed_module",
    ("not/a/python/module", ["unhashable", "metadata"]),
    ids=("invalid-path", "unhashable"),
)
def test_non_plugin_compatible_manual_class_target_is_safely_absent(
    monkeypatch: pytest.MonkeyPatch,
    malformed_module: object,
) -> None:
    expected_registry = ParserRegistry()
    expected_registry.register(_ParserA())
    expected_fingerprint = expected_registry.snapshot().fingerprint
    original_module = _ParserA.__module__
    original_qualname = _ParserA.__qualname__
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module=original_module,
        attribute=original_qualname,
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    monkeypatch.setattr(_ParserA, "__module__", malformed_module)
    registry = ParserRegistry()

    registered = registry.register(_ParserA())
    report = registry.discover_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    )

    assert registered.fingerprint == expected_fingerprint
    assert report.descriptors == (descriptor,)
    assert report.failures == ()


def test_mixed_manual_plugin_snapshot_is_operation_order_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module="vendor.valid",
        attribute="Parser",
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    policy = ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))

    manual_first = ParserRegistry()
    manual_first.register(_ParserA())
    manual_first.discover_plugins(policy)
    plugin_first = ParserRegistry()
    plugin_first.discover_plugins(policy)
    plugin_first.register(_ParserA())

    assert manual_first.snapshot() == plugin_first.snapshot()


def test_duplicate_adapter_id_is_rejected_without_mutating_registry() -> None:
    registry = ParserRegistry()
    original = _ParserA()
    registry.register(original)
    before = registry.snapshot()

    with pytest.raises(ParserError) as raised:
        registry.register(_ParserA())

    assert raised.value.error_code == "PARSER_DUPLICATE_REGISTRATION"
    assert raised.value.details["duplicate_kind"] == "adapter_id"
    assert registry.snapshot() == before


def test_bulk_duplicate_failure_rolls_back_entire_manual_batch() -> None:
    registry = ParserRegistry()
    registry.register(_ParserA())
    before = registry.snapshot()

    with pytest.raises(ParserError):
        registry.register_many((_ParserB(), _ParserA()))

    assert registry.snapshot() == before


def test_bulk_rejects_internal_duplicate_id_and_instance_without_mutation() -> None:
    duplicate_id_registry = ParserRegistry()

    with pytest.raises(ParserError) as duplicate_id:
        duplicate_id_registry.register_many((_ParserA(), _ParserA()))

    assert duplicate_id.value.details["duplicate_kind"] == "adapter_id"
    assert duplicate_id_registry.snapshot().parsers == ()

    duplicate_instance_registry = ParserRegistry()
    parser = _ParserA()

    def mutate_between_yields() -> Iterator[_ParserA]:
        yield parser
        parser.adapter_id = "parser.alias"
        yield parser

    with pytest.raises(ParserError) as duplicate_instance:
        duplicate_instance_registry.register_many(mutate_between_yields())

    assert duplicate_instance.value.details["duplicate_kind"] == "parser_instance"
    assert duplicate_instance_registry.snapshot().parsers == ()


def test_concurrent_same_id_registration_has_exactly_one_winner() -> None:
    registry = ParserRegistry()
    start = threading.Barrier(2)

    def register(parser: _ParserA) -> str:
        start.wait(timeout=5)
        try:
            registry.register(parser)
        except ParserError as error:
            return error.error_code
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(register, (_ParserA(), _ParserA()), timeout=5))

    assert sorted(outcomes) == ["PARSER_DUPLICATE_REGISTRATION", "accepted"]
    assert tuple(item.adapter_id for item in registry.snapshot().parsers) == (
        "parser.a",
    )


def test_same_parser_instance_with_another_claimed_id_is_rejected_explicitly() -> None:
    registry = ParserRegistry()
    original = _ParserA()
    registry.register(original)
    original.adapter_id = "parser.alias"

    with pytest.raises(ParserError) as raised:
        registry.register(original)

    assert raised.value.error_code == "PARSER_DUPLICATE_REGISTRATION"
    assert raised.value.details["duplicate_kind"] == "parser_instance"


def test_same_configurable_class_is_allowed_for_distinct_adapter_ids() -> None:
    registry = ParserRegistry()
    first = _ParserA()
    second = _ParserA()
    second.adapter_id = "parser.alias"

    registry.register_many((first, second))

    assert tuple(item.adapter_id for item in registry.snapshot().parsers) == (
        "parser.a",
        "parser.alias",
    )


@pytest.mark.anyio
async def test_open_session_freezes_mutation_and_closes_cleanly() -> None:
    registry = ParserRegistry()
    registry.register(_ParserA())

    async with registry.session():
        with pytest.raises(ParserError) as raised:
            registry.register(_ParserB())

        assert raised.value.error_code == "PARSER_REGISTRY_FROZEN"

    registry.register(_ParserB())
    assert len(registry.snapshot().parsers) == 2


@pytest.mark.anyio
async def test_session_preserves_body_error_and_every_sanitized_close_failure() -> None:
    source, probe_context, parse_context = _runtime_source_contexts()
    first = _FailOnceCloseIterator("password=DO_NOT_LEAK_FIRST_CLOSE")
    second = _FailOnceCloseIterator("password=DO_NOT_LEAK_SECOND_CLOSE")
    registry = ParserRegistry()
    registry.register(_CloseScriptedParser((first, second)))
    body_failure = ValueError("consumer failed")

    with pytest.raises(ExceptionGroup) as raised:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            streams = (
                selected.parse(source, parse_context),
                selected.parse(source, parse_context),
            )
            raise body_failure

    assert raised.value.exceptions[0] is body_failure
    close_failures = tuple(
        error for error in raised.value.exceptions[1:] if isinstance(error, ParserError)
    )
    assert len(close_failures) == 2
    assert all(error.error_code == "PARSER_OUTPUT_INVALID" for error in close_failures)
    assert all(
        error.details["reason"] == "iterator_close_failed" for error in close_failures
    )
    rendered = "".join(traceback.format_exception(raised.value))
    assert "DO_NOT_LEAK_FIRST_CLOSE" not in rendered
    assert "DO_NOT_LEAK_SECOND_CLOSE" not in rendered

    registry.register(_ParserB())
    for stream, iterator in zip(streams, (first, second), strict=True):
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        assert iterator.close_attempts == 2
        assert iterator.closed is True


@pytest.mark.anyio
async def test_cancelled_exit_preserves_cancellation_and_all_close_failures() -> None:
    source, probe_context, parse_context = _runtime_source_contexts()
    blocked = _FailOnceCloseIterator(
        "password=DO_NOT_LEAK_BLOCKED_CLOSE",
        blocked=True,
    )
    sibling = _FailOnceCloseIterator("password=DO_NOT_LEAK_SIBLING_CLOSE")
    registry = ParserRegistry()
    registry.register(_CloseScriptedParser((blocked, sibling)))
    session = registry.session()
    await session.__aenter__()
    selected = await session.select(source, probe_context)
    streams = (
        selected.parse(source, parse_context),
        selected.parse(source, parse_context),
    )

    exit_task = asyncio.create_task(session.__aexit__(None, None, None))
    await blocked.close_started.wait()
    exit_task.cancel()
    blocked.close_release.set()

    with pytest.raises(BaseExceptionGroup) as raised:
        await exit_task

    assert isinstance(raised.value.exceptions[0], asyncio.CancelledError)
    close_failures = tuple(
        error for error in raised.value.exceptions[1:] if isinstance(error, ParserError)
    )
    assert len(close_failures) == 2
    assert all(
        error.details["reason"] == "iterator_close_failed" for error in close_failures
    )
    rendered = "".join(traceback.format_exception(raised.value))
    assert "DO_NOT_LEAK_BLOCKED_CLOSE" not in rendered
    assert "DO_NOT_LEAK_SIBLING_CLOSE" not in rendered

    registry.register(_ParserB())
    for stream, iterator in zip(streams, (blocked, sibling), strict=True):
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        assert iterator.close_attempts == 2
        assert iterator.closed is True


@pytest.mark.anyio
async def test_grouped_close_failure_is_sanitized_without_skipping_sibling() -> None:
    source, probe_context, parse_context = _runtime_source_contexts()
    grouped = _GroupedCloseIterator()
    sibling = _SuccessfulCloseIterator()
    registry = ParserRegistry()
    registry.register(_CloseScriptedParser((grouped, sibling)))
    body_failure = ValueError("consumer failed")

    with pytest.raises(BaseExceptionGroup) as raised:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            streams = (
                selected.parse(source, parse_context),
                selected.parse(source, parse_context),
            )
            raise body_failure

    assert raised.value.exceptions[0] is body_failure
    cancellations = tuple(
        error
        for error in raised.value.exceptions[1:]
        if isinstance(error, asyncio.CancelledError)
    )
    close_failures = tuple(
        error for error in raised.value.exceptions[1:] if isinstance(error, ParserError)
    )
    assert len(cancellations) == 1
    assert cancellations[0].args == ()
    assert cancellations[0].__context__ is None
    assert cancellations[0].__cause__ is None
    assert getattr(cancellations[0], "__notes__", ()) == ()
    assert len(close_failures) == 2
    assert all(
        error.details["reason"] == "iterator_close_failed" for error in close_failures
    )
    assert all(error.retryable is True for error in close_failures)
    rendered = "".join(traceback.format_exception(raised.value))
    assert "DO_NOT_LEAK" not in rendered
    assert sibling.closed is True

    registry.register(_ParserB())
    for stream in streams:
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    await streams[0].aclose()

    assert grouped.close_attempts == 2
    assert grouped.closed is True


@pytest.mark.anyio
async def test_repeated_exit_cancellation_keeps_one_sanitized_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "password=DO_NOT_LEAK_REPEATED_CANCELLATION"
    shield_calls = 0

    def scripted_shield(awaitable: Awaitable[object]) -> Awaitable[object]:
        async def wait() -> object:
            nonlocal shield_calls
            shield_calls += 1
            if shield_calls <= 3:
                raise _cancellation_with_context(marker)
            return await awaitable

        return wait()

    monkeypatch.setattr(asyncio, "shield", scripted_shield)
    registry = ParserRegistry()
    session = registry.session()
    await session.__aenter__()

    with pytest.raises(asyncio.CancelledError) as raised:
        await session.__aexit__(None, None, None)

    assert shield_calls == 4
    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.args == ()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in "".join(traceback.format_exception(raised.value))
    registry.register(_ParserA())


@pytest.mark.anyio
async def test_session_body_cancellation_is_sanitized_and_releases_lease() -> None:
    marker = "password=DO_NOT_LEAK_BODY_CANCELLATION"
    registry = ParserRegistry()

    with pytest.raises(asyncio.CancelledError) as raised:
        async with registry.session():
            raise _cancellation_with_context(marker)

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.args == ()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in "".join(traceback.format_exception(raised.value))
    registry.register(_ParserA())


@pytest.mark.anyio
async def test_body_cancellation_with_close_failure_detaches_group_context() -> None:
    marker = "password=DO_NOT_LEAK_BODY_CANCELLATION_GROUP"
    source, probe_context, parse_context = _runtime_source_contexts()
    iterator = _FailOnceCloseIterator(marker)
    registry = ParserRegistry()
    registry.register(_CloseScriptedParser((iterator,)))

    with pytest.raises(BaseExceptionGroup) as raised:
        async with registry.session() as session:
            selected = await session.select(source, probe_context)
            stream = selected.parse(source, parse_context)
            raise _cancellation_with_context(marker)

    group = raised.value
    cancellations = tuple(
        error for error in group.exceptions if isinstance(error, asyncio.CancelledError)
    )
    close_failures = tuple(
        error for error in group.exceptions if isinstance(error, ParserError)
    )
    assert group.__context__ is None
    assert group.__cause__ is None
    assert getattr(group, "__notes__", ()) == ()
    assert len(cancellations) == 1
    assert cancellations[0].args == ()
    assert cancellations[0].__context__ is None
    assert cancellations[0].__cause__ is None
    assert len(close_failures) == 1
    assert close_failures[0].retryable is True
    assert marker not in "".join(traceback.format_exception(group))

    registry.register(_ParserB())
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()


@pytest.mark.anyio
async def test_frozen_registry_does_not_touch_rejected_parser_object() -> None:
    registry = ParserRegistry()
    registry.register(_ParserA())
    candidate = _ObservableParser()

    async with registry.session():
        with pytest.raises(ParserError) as raised:
            registry.register(candidate)

    assert raised.value.error_code == "PARSER_REGISTRY_FROZEN"
    assert candidate.identity_reads == 0


@pytest.mark.anyio
async def test_registry_session_is_single_use_and_old_handles_never_reactivate() -> (
    None
):
    registry = ParserRegistry()
    registry.register(_ParserA())
    session = registry.session()

    async with session:
        pass
    registry.register(_ParserB())

    with pytest.raises(ParserError) as raised:
        async with session:
            pass

    assert raised.value.error_code == "PARSER_SESSION_CLOSED"


@pytest.mark.parametrize(
    "priority",
    [True, 0.0, -(2**31) - 1, 2**31],
)
def test_invalid_priority_is_rejected(priority: object) -> None:
    parser = _ParserA()
    parser.priority = cast(int, priority)

    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(parser)

    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        ("adapter_id", 1, "adapter_id_type"),
        ("adapter_id", "", "adapter_id_format"),
        ("adapter_id", "a" * 129, "adapter_id_format"),
        ("adapter_id", "parser\x00a", "adapter_id_format"),
        ("version", 1, "version_type"),
        ("version", "", "version_format"),
        ("version", "v" * 129, "version_format"),
        ("version", "1.0\u202e.0", "version_format"),
    ),
)
def test_registration_rejects_invalid_identity_fields(
    field: str,
    value: object,
    reason: str,
) -> None:
    parser = _ParserA()
    setattr(parser, field, value)

    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(parser)

    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"
    assert raised.value.details["reason"] == reason


def test_registration_reads_identity_once_and_never_reinspects_snapshot() -> None:
    parser = _ObservableParser()
    registry = ParserRegistry()

    registry.register(parser)
    first = registry.snapshot()
    second = registry.snapshot()

    assert parser.identity_reads == 1
    assert first == second


def test_identity_getter_failure_is_typed_and_does_not_leak_value() -> None:
    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(_ExplodingIdentityParser())

    rendered = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            repr(raised.value.details),
            repr(raised.value.__context__),
            repr(raised.value.__cause__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"
    assert raised.value.details["reason"] == "identity_access"
    assert raised.value.cause == "RuntimeError"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_IDENTITY_GETTER" not in rendered


def test_protocol_inspection_failure_does_not_retain_raw_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(registry_module, "Parser", _ExplodingProtocol)

    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(_ParserA())

    rendered = " ".join(
        (
            repr(raised.value),
            repr(raised.value.details),
            repr(raised.value.__context__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"
    assert raised.value.details["reason"] == "protocol_inspection"
    assert raised.value.cause == "RuntimeError"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_PROTOCOL_INSPECTION" not in rendered


@pytest.mark.parametrize("member", ("probe", "parse"))
def test_registration_rejects_non_callable_parser_methods(member: str) -> None:
    parser = _ParserA()
    setattr(parser, member, object())

    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(parser)

    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"


@pytest.mark.parametrize(
    "adapter_id",
    ("Parser.A", " parser.a", "parser.a ", "p\u0430rser.a"),
)
def test_noncanonical_manual_id_cannot_take_over_existing_registration(
    adapter_id: str,
) -> None:
    registry = ParserRegistry()
    registry.register(_ParserA())
    before = registry.snapshot()
    challenger = _ParserA()
    challenger.adapter_id = adapter_id

    with pytest.raises(ParserError) as raised:
        registry.register(challenger)

    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"
    assert registry.snapshot() == before


@pytest.mark.parametrize(
    ("version", "priority"),
    (("2.0.0", 10), ("1.0.0", 2**31 - 1)),
)
def test_version_or_priority_cannot_take_over_existing_adapter_id(
    version: str,
    priority: int,
) -> None:
    registry = ParserRegistry()
    registry.register(_ParserA())
    before = registry.snapshot()
    challenger = _ParserA()
    challenger.version = version
    challenger.priority = priority

    with pytest.raises(ParserError) as raised:
        registry.register(challenger)

    assert raised.value.error_code == "PARSER_DUPLICATE_REGISTRATION"
    assert registry.snapshot() == before


@pytest.mark.parametrize("field", ("adapter_id", "version"))
def test_manual_registration_rejects_credential_canary_identity(field: str) -> None:
    parser = _ParserA()
    setattr(parser, field, "sk-supersecret")

    with pytest.raises(ParserError) as raised:
        ParserRegistry().register(parser)

    assert raised.value.error_code == "PARSER_INVALID_ADAPTER"
    assert "sk-supersecret" not in repr(raised.value.details)


def test_discovery_registers_valid_descriptors_and_reports_registry_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = ParserPluginDescriptor(
        adapter_id="parser.a",
        distribution_name="collision-plugin",
        distribution_version="1.0.0",
        module="vendor.collision",
        attribute="Parser",
    )
    valid = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module="vendor.valid",
        attribute="Parser",
    )

    def discover(policy: ParserDiscoveryPolicy) -> ParserPluginDiscoveryReport:
        assert policy.allowed_distributions == (
            "collision-plugin",
            "valid-plugin",
        )
        return ParserPluginDiscoveryReport(
            descriptors=(collision, valid),
            failures=(),
        )

    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        discover,
    )
    registry = ParserRegistry()
    registry.register(_ParserA())

    report = registry.discover_plugins(
        ParserDiscoveryPolicy(
            allowed_distributions=("valid-plugin", "collision-plugin"),
        )
    )

    assert report.descriptors == (valid,)
    assert tuple(item.adapter_id for item in registry.snapshot().plugins) == (
        "parser.plugin",
    )
    assert len(report.failures) == 1
    assert report.failures[0].adapter_id == "parser.a"
    assert report.failures[0].error.error_code == "PARSER_DUPLICATE_REGISTRATION"


def test_discovered_target_cannot_duplicate_registered_parser_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module=_ParserA.__module__,
        attribute=_ParserA.__qualname__,
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    registry = ParserRegistry()
    registry.register(_ParserA())

    report = registry.discover_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    )

    assert report.descriptors == ()
    assert report.failures[0].error.details["duplicate_kind"] == "parser_class"


@pytest.mark.parametrize("duplicate_kind", ("adapter_id", "parser_class"))
def test_manual_registration_cannot_take_over_discovered_plugin_claim(
    monkeypatch: pytest.MonkeyPatch,
    duplicate_kind: str,
) -> None:
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.a" if duplicate_kind == "adapter_id" else "parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module="vendor.valid"
        if duplicate_kind == "adapter_id"
        else _ParserA.__module__,
        attribute="Parser" if duplicate_kind == "adapter_id" else _ParserA.__qualname__,
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    registry = ParserRegistry()
    registry.discover_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    )
    before = registry.snapshot()
    candidate = _ParserA()
    if duplicate_kind == "parser_class":
        candidate.adapter_id = "parser.manual"

    with pytest.raises(ParserError) as raised:
        registry.register(candidate)

    assert raised.value.error_code == "PARSER_DUPLICATE_REGISTRATION"
    assert raised.value.details["duplicate_kind"] == duplicate_kind
    assert registry.snapshot() == before


@pytest.mark.anyio
async def test_active_session_rejects_discovery_before_metadata_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery_called = False

    def forbidden_discovery(
        policy: ParserDiscoveryPolicy,
    ) -> ParserPluginDiscoveryReport:
        nonlocal discovery_called
        discovery_called = True
        raise AssertionError("metadata discovery было вызвано")

    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        forbidden_discovery,
    )
    registry = ParserRegistry()

    async with registry.session():
        with pytest.raises(ParserError) as raised:
            registry.discover_plugins(
                ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
            )

    assert raised.value.error_code == "PARSER_REGISTRY_FROZEN"
    assert discovery_called is False


@pytest.mark.anyio
async def test_discovery_rolls_back_when_session_opens_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module="vendor.valid",
        attribute="Parser",
    )
    discovery_started = threading.Event()
    discovery_release = threading.Event()

    def blocked_discovery(
        policy: ParserDiscoveryPolicy,
    ) -> ParserPluginDiscoveryReport:
        discovery_started.set()
        assert discovery_release.wait(timeout=5)
        return ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        )

    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        blocked_discovery,
    )
    registry = ParserRegistry()
    policy = ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    discovery_task = asyncio.create_task(
        asyncio.to_thread(registry.discover_plugins, policy)
    )
    assert await asyncio.to_thread(discovery_started.wait, 5)
    session = registry.session()
    await session.__aenter__()
    try:
        discovery_release.set()
        with pytest.raises(ParserError) as raised:
            await discovery_task

        assert raised.value.error_code == "PARSER_REGISTRY_FROZEN"
        assert registry.snapshot().plugins == ()
    finally:
        discovery_release.set()
        await session.__aexit__(None, None, None)


def test_plugin_activation_requires_future_sandbox_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = ParserPluginDescriptor(
        adapter_id="parser.plugin",
        distribution_name="valid-plugin",
        distribution_version="1.0.0",
        module="vendor.valid",
        attribute="Parser",
    )
    monkeypatch.setattr(
        "structuraguard.parsers.discovery.discover_parser_plugins",
        lambda policy: ParserPluginDiscoveryReport(
            descriptors=(descriptor,),
            failures=(),
        ),
    )
    registry = ParserRegistry()
    registry.discover_plugins(
        ParserDiscoveryPolicy(allowed_distributions=("valid-plugin",))
    )

    with pytest.raises(SecurityPolicyError) as raised:
        registry.activate_plugin("parser.plugin")

    assert raised.value.error_code == "SECURITY_SANDBOX_REQUIRED"
    assert raised.value.details["adapter_id"] == "parser.plugin"
