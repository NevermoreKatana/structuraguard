"""Отмена/timeout доходят до активного I/O port, а lease проходит cleanup."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
from tests.fakes.pipeline import (
    FakeDatabase,
    FakeParser,
    Inspector,
    Stream,
    defaults,
    engine,
)

from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts.constraint_validation import (
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunRequest,
    LoadRequest,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.reports import AuditEvent
from structuraguard.contracts.security import SecurityLimits
from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.pipeline import DatabaseBinding, SourceRequest
from structuraguard.ports.source import ParseContext, ProbeContext
from structuraguard.security.session import SecuritySession


class Barrier:
    def __init__(self, port: str) -> None:
        self.port = port
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()

    async def wait(self, port: str) -> None:
        if port == self.port:
            self.entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.exited.set()


class WaitingParser(FakeParser):
    def __init__(self, barrier: Barrier) -> None:
        super().__init__()
        self.barrier = barrier

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        await self.barrier.wait("probe")
        return await super().probe(source, context)

    async def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        try:
            await self.barrier.wait("parser")
            async for batch in super().parse(source, context):
                yield batch
        finally:
            self.closed = True


class WaitingInspector(Inspector):
    def __init__(self, catalog: DatabaseCatalog, barrier: Barrier) -> None:
        super().__init__(catalog)
        self.barrier = barrier

    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        await self.barrier.wait("inspector")
        return await super().inspect(request)


class WaitingDatabase(FakeDatabase):
    def __init__(self, barrier: Barrier) -> None:
        super().__init__()
        self.barrier = barrier
        self.inspector = WaitingInspector(self.catalog, barrier)
        self.resources: SecuritySession | None = None

    def bind(self, resources: SecuritySession) -> DatabaseBinding:
        self.resources = resources
        return super().bind(resources)

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        await self.barrier.wait("reader")
        return await super().read(request, catalog=catalog)

    async def plan(self, request: DryRunRequest) -> DryRunExecutionPlan:
        await self.barrier.wait("planner")
        return await super().plan(request)

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        await self.barrier.wait("loader")
        return await super().execute(request)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "port", ("probe", "parser", "inspector", "reader", "planner", "loader")
)
async def test_cancellation_reaches_active_port_and_closes_run(port: str) -> None:
    barrier = Barrier(port)
    db, parser = WaitingDatabase(barrier), WaitingParser(barrier)
    events: list[AuditEvent] = []

    async def observe(event: AuditEvent) -> None:
        events.append(event)

    sdk = engine(dependencies=replace(defaults(db), hooks=(observe,)), parser=parser)
    task = asyncio.create_task(
        sdk.ingest(
            SourceRequest(stream=Stream(), display_name="input.json"),
            dry_run=port == "planner",
        )
    )
    try:
        async with asyncio.timeout(3):
            await barrier.entered.wait()
            task.cancel("private-cancellation-canary")
            with pytest.raises(asyncio.CancelledError) as captured:
                await task
            assert not captured.value.args
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert barrier.exited.is_set()
    assert events[-1].status is S.CANCELLED
    assert db.writes == 0
    assert port == "probe" or parser.closed


@pytest.mark.anyio
@pytest.mark.parametrize("port", ("probe", "parser", "inspector", "reader", "planner"))
async def test_deadline_cancels_read_only_port(port: str) -> None:
    barrier = Barrier(port)
    db = WaitingDatabase(barrier)
    original = defaults(db)
    deps = replace(
        original,
        security=original.security.model_copy(
            update={
                "limits": SecurityLimits(max_processing_time_ms=500),
            }
        ),
    )
    result = await engine(dependencies=deps, parser=WaitingParser(barrier)).ingest(
        SourceRequest(stream=Stream(), display_name="input.json"),
        dry_run=True,
    )
    assert barrier.entered.is_set() and barrier.exited.is_set()
    assert result.status is S.FAILED
    assert result.errors[-1].code == "PROCESSING_TIMEOUT"
    assert db.writes == 0
