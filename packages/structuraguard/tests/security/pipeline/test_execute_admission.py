"""Отклонённый execute не меняет режим активной операции и не разрешает запись."""

import asyncio
from dataclasses import replace

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import FakeDatabase, engine, request
from tests.unit.pipeline.test_acceptance_lifecycle import repeatable

from structuraguard import StructuraGuardError
from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts import TransactionOutcome
from structuraguard.contracts.reports import AuditEvent
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.stores import MemoryStagingStore


@pytest.fixture
def database() -> FakeDatabase:
    db = FakeDatabase()
    db.store = MemoryStagingStore(
        target_id=db.catalog.target_id,
        retention=StagingRetentionPolicy(),
        clock=fixed_clock,
    )
    return db


@pytest.mark.anyio
@pytest.mark.parametrize("dry_run", (True, False))
@pytest.mark.parametrize("pause", ("stage_started", "run_finished"))
async def test_rejected_execute_preserves_active_mode(
    dry_run: bool, pause: str, database: FakeDatabase
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(event: AuditEvent) -> None:
        if event.event_type == pause and event.status is (
            S.STAGING if pause == "stage_started" else S.COMPLETED
        ):
            entered.set()
            await release.wait()

    db = database
    sdk = engine(dependencies=replace(repeatable(db), hooks=(hook,)))
    source = await sdk.inspect_source(request())
    async with source:
        parse = await sdk.create_parse_plan(source)
        assert parse
        data = await sdk.parse_semantically(source, plan=parse)
        mapping = await sdk.create_mapping_plan(data)
        assert mapping.plan
        task = asyncio.create_task(
            sdk.execute(data, plan=mapping.plan, dry_run=dry_run)
        )
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                before = source._run.snapshot()
                with pytest.raises(StructuraGuardError, match="SDK_RUN_BUSY"):
                    await sdk.execute(data, plan=mapping.plan, dry_run=not dry_run)
                assert source._run.snapshot() == before
        finally:
            release.set()
            result = await task
        assert result.status is S.COMPLETED
        assert result.dry_run is dry_run
        assert result.transaction_outcome is (
            TransactionOutcome.DRY_RUN if dry_run else TransactionOutcome.COMMITTED
        )
        assert result.load_report and result.load_report.loaded_records == (
            0 if dry_run else 2
        )
        assert db.writes == (0 if dry_run else 1)


@pytest.mark.anyio
@pytest.mark.parametrize("pause", ("stage_started", "run_finished"))
async def test_reentrant_execute_is_rejected_without_changing_dry_run(
    pause: str, database: FakeDatabase
) -> None:
    rejected: list[str] = []

    async def hook(event: AuditEvent) -> None:
        if event.event_type == pause and event.status is (
            S.STAGING if pause == "stage_started" else S.COMPLETED
        ):
            assert mapping.plan
            with pytest.raises(StructuraGuardError, match="SDK_RUN_BUSY") as error:
                await sdk.execute(data, plan=mapping.plan, dry_run=False)
            rejected.append(error.value.error_code)

    db = database
    sdk = engine(dependencies=replace(repeatable(db), hooks=(hook,)))
    source = await sdk.inspect_source(request())
    async with source:
        parse = await sdk.create_parse_plan(source)
        assert parse
        data = await sdk.parse_semantically(source, plan=parse)
        mapping = await sdk.create_mapping_plan(data)
        assert mapping.plan
        result = await sdk.execute(data, plan=mapping.plan, dry_run=True)
    assert rejected == ["SDK_RUN_BUSY"]
    assert result.status is S.COMPLETED and result.dry_run
    assert result.transaction_outcome is TransactionOutcome.DRY_RUN
    assert not result.errors and db.writes == 0


@pytest.mark.anyio
async def test_execute_cannot_change_mode_while_another_stage_is_busy(
    database: FakeDatabase,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(event: AuditEvent) -> None:
        if (
            event.event_type == "stage_started"
            and event.status is S.MAPPING_PLAN_VALIDATING
        ):
            entered.set()
            await release.wait()

    db = database
    sdk = engine(dependencies=replace(repeatable(db), hooks=(hook,)))
    source = await sdk.inspect_source(request())
    async with source:
        parse = await sdk.create_parse_plan(source)
        assert parse
        data = await sdk.parse_semantically(source, plan=parse)
        mapping = await sdk.create_mapping_plan(data)
        assert mapping.plan
        task = asyncio.create_task(sdk.validate_mapping_plan(data, plan=mapping.plan))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                before = source._run.snapshot()
                with pytest.raises(StructuraGuardError, match="SDK_RUN_BUSY"):
                    await sdk.execute(data, plan=mapping.plan, dry_run=False)
                assert source._run.snapshot() == before
        finally:
            release.set()
            await task
        # Отклонённая попытка не удерживает execution admission после завершения stage.
        result = await sdk.execute(data, plan=mapping.plan, dry_run=True)
        assert result.status is S.COMPLETED and result.dry_run
        assert db.writes == 0
