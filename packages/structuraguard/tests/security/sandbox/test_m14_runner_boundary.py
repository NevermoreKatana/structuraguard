import asyncio
from uuid import UUID

import pytest
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts.plugins import ParserPluginDescriptor
from structuraguard.contracts.sandbox import (
    SandboxCapabilities,
    SandboxExit,
    SandboxParserSpec,
    SandboxPolicy,
    SandboxRequest,
)
from structuraguard.contracts.security import SecurityLimits, SecurityPolicy
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.builtin import PlainTextParser
from structuraguard.parsers.runners import InProcessParserRunner, SandboxParserRunner
from structuraguard.ports.sandbox import ParserRunner
from structuraguard.ports.source import SourceReader
from structuraguard.security import SecuritySession


class Process:
    def __init__(self, probe: bytes, frames: list[bytes]) -> None:
        self.probe_frame = probe
        self.frames = frames
        self.closed = False
        self.entered = asyncio.Event()
        self.hang = False
        self.cleanup_ok = True
        self.sizes: list[int] = []

    async def probe(self, *, max_bytes: int) -> bytes:
        self.sizes.append(max_bytes)
        return self.probe_frame

    async def read_frame(self, *, max_bytes: int) -> bytes | None:
        self.sizes.append(max_bytes)
        if self.hang:
            self.entered.set()
            await asyncio.Event().wait()
        return self.frames.pop(0) if self.frames else None

    async def wait(self) -> SandboxExit:
        return SandboxExit(status="completed", cleanup_complete=self.cleanup_ok)

    async def aclose(self) -> SandboxExit:
        self.closed = True
        return SandboxExit(status="terminated", cleanup_complete=self.cleanup_ok)


class Backend:
    capabilities = SandboxCapabilities.model_validate(
        {name: True for name in SandboxCapabilities.model_fields}
    )

    def __init__(self, process: Process) -> None:
        self.process = process
        self.started = False
        self.request: SandboxRequest | None = None

    async def start(self, request: SandboxRequest, reader: SourceReader) -> Process:
        self.started = True
        self.request = request
        return self.process


def spec() -> SandboxParserSpec:
    parser = PlainTextParser()
    return SandboxParserSpec(
        descriptor=ParserPluginDescriptor(
            adapter_id=parser.adapter_id,
            distribution_name="fixture-parser",
            distribution_version="1.0.0",
            module="never_import_this",
            attribute="Parser",
        ),
        artifact_fingerprint="sha256:" + "c" * 64,
        parser_version=parser.version,
        format_id="txt",
    )


async def fixture_process() -> Process:
    data = b"bounded text\n"
    source = source_for(data, display_name="fixture.txt")
    probe_context, parse_context = contexts_for(source, data)
    parser = PlainTextParser()
    probe = await parser.probe(source, probe_context)
    return Process(
        probe.model_dump_json().encode(),
        [
            batch.model_dump_json().encode()
            async for batch in parser.parse(source, parse_context)
        ],
    )


def runner(
    backend: Backend | None, policy: SandboxPolicy | None = None
) -> SandboxParserRunner:
    return SandboxParserRunner(
        spec(),
        policy=policy or SandboxPolicy(allowed_specs=(spec().fingerprint,)),
        session=SecuritySession(
            SecurityPolicy(allowed_formats=("txt",)), run_id=UUID(int=1)
        ),
        backend=backend,
        run_id=UUID(int=1),
    )


async def collect(value: ParserRunner) -> list[ExtractedBatch]:
    data = b"bounded text\n"
    source = source_for(data, display_name="fixture.txt")
    async with value.parse(source, contexts_for(source, data)[1]) as batches:
        return [batch async for batch in batches]


@pytest.mark.anyio
async def test_runners_share_existing_validation_contract() -> None:
    process = await fixture_process()
    backend = Backend(process)
    sandboxed = await collect(runner(backend))
    local = await collect(
        InProcessParserRunner(
            PlainTextParser(),
            session=SecuritySession(
                SecurityPolicy(allowed_formats=("txt",), parser_trust="trusted"),
                run_id=UUID(int=1),
            ),
        )
    )
    assert [b.lines for b in sandboxed] == [b.lines for b in local]
    assert [b.source for b in sandboxed] == [b.source for b in local]
    assert sandboxed[-1].is_last and local[-1].is_last
    assert process.closed and backend.request is not None
    assert set(backend.request.model_dump()) == {
        "version",
        "run_id",
        "parser",
        "source",
        "source_size_bytes",
        "limits",
        "output_policy",
    }


@pytest.mark.anyio
@pytest.mark.parametrize("missing", tuple(SandboxCapabilities.model_fields))
async def test_every_missing_capability_denies_before_backend_start(
    missing: str,
) -> None:
    backend = Backend(await fixture_process())
    backend.capabilities = backend.capabilities.model_copy(update={missing: False})
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        await collect(runner(backend))
    assert not backend.started


@pytest.mark.anyio
async def test_no_backend_or_unapproved_pinned_artifact_denied() -> None:
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        await collect(runner(None))
    backend = Backend(await fixture_process())
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        await collect(runner(backend, SandboxPolicy()))
    assert not backend.started


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_total_result_bytes_boundary_and_one_over(extra: int) -> None:
    process = await fixture_process()
    size = len(process.probe_frame) + sum(map(len, process.frames))
    value = runner(
        Backend(process),
        SandboxPolicy(
            allowed_specs=(spec().fingerprint,), max_result_bytes=size - extra
        ),
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect(value)
    else:
        assert await collect(value)
    assert process.closed


@pytest.mark.anyio
async def test_forged_source_provenance_rejected_and_cleaned() -> None:
    process = await fixture_process()
    process.frames[0] = process.frames[0].replace(b"source-1", b"source-2")
    with pytest.raises(SecurityPolicyError, match="PARSER_OUTPUT_INVALID"):
        await collect(runner(Backend(process)))
    assert process.closed


@pytest.mark.anyio
async def test_cancel_during_read_terminates_worker_without_exception_payload() -> None:
    process = await fixture_process()
    process.hang = True
    task = asyncio.create_task(collect(runner(Backend(process))))
    await process.entered.wait()
    task.cancel("raw-secret-canary")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert process.closed
    assert "raw-secret-canary" not in str(error.value)


@pytest.mark.anyio
async def test_strict_configured_risky_parser_never_reads_source() -> None:
    class Reader:
        source_fingerprint = "sha256:" + "b" * 64

        async def read(self, *, offset: int, size: int) -> bytes:
            raise AssertionError("source read forbidden")

    data = b"bounded text\n"
    source = source_for(data, display_name="fixture.txt")
    context = contexts_for(source, data)[1]
    value = InProcessParserRunner(
        PlainTextParser(),
        session=SecuritySession(
            SecurityPolicy(
                allowed_formats=("txt",),
                parser_trust="trusted",
                strict_mode=True,
                risky_formats=("txt",),
            ),
            run_id=UUID(int=1),
        ),
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        async with value.parse(source, context):
            raise AssertionError("unreachable")


@pytest.mark.anyio
async def test_deadline_terminates_hanging_worker() -> None:
    process = await fixture_process()
    process.hang = True
    session = SecuritySession(
        SecurityPolicy(
            allowed_formats=("txt",), limits=SecurityLimits(max_parser_time_ms=20)
        ),
        run_id=UUID(int=1),
    )
    value = SandboxParserRunner(
        spec(),
        policy=SandboxPolicy(allowed_specs=(spec().fingerprint,)),
        session=session,
        backend=Backend(process),
        run_id=UUID(int=1),
    )
    with pytest.raises(SecurityPolicyError, match="PROCESSING_TIMEOUT"):
        await asyncio.wait_for(collect(value), timeout=2)
    assert process.closed


@pytest.mark.anyio
async def test_cleanup_ack_required_after_success_and_early_exit() -> None:
    process = await fixture_process()
    process.cleanup_ok = False
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_CLEANUP_FAILED"):
        await collect(runner(Backend(process)))
    assert process.closed
    process = await fixture_process()
    value = runner(Backend(process))
    data = b"bounded text\n"
    source = source_for(data, display_name="fixture.txt")
    async with value.parse(source, contexts_for(source, data)[1]) as batches:
        await anext(batches)
    assert process.closed


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_frame_size_exact_and_one_over(extra: int) -> None:
    process = await fixture_process()
    largest = max(len(process.probe_frame), *(len(frame) for frame in process.frames))
    value = runner(
        Backend(process),
        SandboxPolicy(
            allowed_specs=(spec().fingerprint,), max_frame_bytes=largest - extra
        ),
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect(value)
    else:
        assert await collect(value)
    assert process.closed


@pytest.mark.anyio
async def test_host_source_context_limit_checked_before_start() -> None:
    from dataclasses import replace

    process = await fixture_process()
    backend = Backend(process)
    value = runner(backend)
    data = b"bounded text\n"
    source = source_for(data, display_name="fixture.txt")
    context = replace(contexts_for(source, data)[1], max_bytes=len(data) - 1)
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        async with value.parse(source, context):
            raise AssertionError("unreachable")
    assert not backend.started


@pytest.mark.anyio
async def test_process_returned_at_deadline_is_still_cleaned() -> None:
    from tests.unit.llm.test_router import Clock

    process = await fixture_process()
    ticks = Clock()

    class LateBackend(Backend):
        async def start(self, request: SandboxRequest, reader: SourceReader) -> Process:
            ticks.seconds = 1
            return await super().start(request, reader)

    session = SecuritySession(
        SecurityPolicy(
            allowed_formats=("txt",), limits=SecurityLimits(max_parser_time_ms=1000)
        ),
        run_id=UUID(int=1),
        monotonic=ticks,
    )
    value = SandboxParserRunner(
        spec(),
        policy=SandboxPolicy(allowed_specs=(spec().fingerprint,)),
        session=session,
        backend=LateBackend(process),
        run_id=UUID(int=1),
    )
    with pytest.raises(SecurityPolicyError, match="PROCESSING_TIMEOUT"):
        await collect(value)
    assert process.closed


@pytest.mark.anyio
async def test_cancel_racing_with_completed_start_adopts_handle() -> None:
    process = await fixture_process()

    class CancellingBackend(Backend):
        async def start(self, request: SandboxRequest, reader: SourceReader) -> Process:
            result = await super().start(request, reader)
            owner.cancel("restricted-start-canary")
            return result

    owner = asyncio.create_task(collect(runner(CancellingBackend(process))))
    with pytest.raises(asyncio.CancelledError) as error:
        await owner
    assert process.closed and not error.value.args


@pytest.mark.anyio
async def test_cancel_pending_start_waits_for_backend_cleanup() -> None:
    process = await fixture_process()
    started = asyncio.Event()

    class PendingBackend(Backend):
        async def start(self, request: SandboxRequest, reader: SourceReader) -> Process:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await process.aclose()
            return process

    task = asyncio.create_task(collect(runner(PendingBackend(process))))
    await started.wait()
    task.cancel("restricted-start-canary")
    with pytest.raises(asyncio.CancelledError) as error:
        await task
    assert process.closed and not error.value.args
