"""Недостающие M15 checkpoints: equivalence, ownership и terminal admission."""

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import FakeDatabase, FakeParser, defaults, engine, request

from structuraguard import StructuraGuard, StructuraGuardError
from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts import TransactionOutcome
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.orchestration import IngestResult
from structuraguard.contracts.reports import AuditEvent
from structuraguard.parsers import ParserRegistry
from structuraguard.pipeline import SDKDependencies
from structuraguard.pipeline.state import check_transition


def repeatable(db: FakeDatabase) -> SDKDependencies:
    ids = iter(
        UUID(f"aaaaaaaa-bbbb-4ccc-addd-eeeeeeee{index:04x}") for index in range(1, 1000)
    )
    return replace(defaults(db), clock=fixed_clock, new_id=lambda: next(ids))


def test_sync_async_and_stepped_results_are_equivalent() -> None:
    async def ingest() -> IngestResult:
        sdk = engine(dependencies=repeatable(FakeDatabase()))
        source = await sdk.inspect_source(request())
        async with source:
            analysis = await sdk.analyze_structure(source)
            plan = await sdk.create_parse_plan(source, structure=analysis)
            assert plan
            await sdk.validate_parse_plan(source, plan=plan)
            data = await sdk.parse_semantically(source, plan=plan)
            await sdk.profile_records(data)
            database = await sdk.inspect_database(source=data)
            proposal = await sdk.create_mapping_plan(data, database=database)
            assert proposal.plan
            await sdk.validate_mapping_plan(data, plan=proposal.plan)
            return await sdk.execute(data, plan=proposal.plan, dry_run=True)

    expected = asyncio.run(ingest())
    registry = ParserRegistry()
    registry.register(FakeParser())
    with StructuraGuard(
        parser_registry=registry, dependencies=repeatable(FakeDatabase())
    ) as sdk:
        source = sdk.inspect_source(request())
        analysis = sdk.analyze_structure(source)
        plan = sdk.create_parse_plan(source, structure=analysis)
        assert plan
        assert sdk.validate_parse_plan(source, plan=plan).validated_plan
        data = sdk.parse_semantically(source, plan=plan)
        profile = sdk.profile_records(data)
        database = sdk.inspect_database(source=data)
        proposal = sdk.create_mapping_plan(data, database=database)
        assert proposal.plan
        checked = sdk.validate_mapping_plan(data, plan=proposal.plan)
        assert (
            isinstance(checked, MappingPlanValidationResult) and checked.validated_plan
        )
        actual = sdk.execute(data, plan=proposal.plan, dry_run=True)
        assert profile == actual.normalized_profile
    # Одинаковые trusted IDs/clocks позволяют сравнить content bindings.
    for name in (
        "status",
        "transaction_outcome",
        "source_fingerprint",
        "extraction_fingerprint",
        "parse_plan_fingerprint",
        "normalized_fingerprint",
        "database_fingerprint",
        "mapping_plan_fingerprint",
        "parse_plan",
        "mapping_plan",
        "validation_report",
        "dry_run_plan",
        "load_report",
        "errors",
    ):
        assert getattr(actual, name) == getattr(expected, name), name


@pytest.mark.anyio
async def test_analysis_and_standalone_inspection_never_load() -> None:
    db = FakeDatabase()
    sdk = engine(db)
    result = await sdk.analyze(request())
    assert result.status is S.COMPLETED and result.mapping_plan
    assert result.load_report is None and result.validation_report is None
    assert result.transaction_outcome is TransactionOutcome.NOT_STARTED
    assert await sdk.inspect_database() == db.catalog
    assert db.inspector.calls == 2
    assert db.writes == db.plans == 0 and not db.artifacts


@pytest.mark.anyio
async def test_completed_source_rejects_second_execution() -> None:
    db = FakeDatabase()
    sdk = engine(db)
    source = await sdk.inspect_source(request())
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan
        data = await sdk.parse_semantically(source, plan=plan)
        mapping = await sdk.create_mapping_plan(data)
        assert mapping.plan
        first = await sdk.execute(data, plan=mapping.plan)
        assert first.status is S.COMPLETED and db.writes == 1
        with pytest.raises(StructuraGuardError, match="SDK_INVALID_TRANSITION"):
            await sdk.execute(data, plan=mapping.plan)
        assert source._run.snapshot() == first
        assert db.writes == 1


@pytest.mark.anyio
async def test_busy_lease_rejects_concurrent_stage_without_poisoning_run() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(event: AuditEvent) -> None:
        if (
            event.status is S.STRUCTURE_PROFILING
            and event.event_type == "stage_started"
        ):
            entered.set()
            await release.wait()

    sdk = engine(dependencies=replace(defaults(), hooks=(hook,)))
    source = await sdk.inspect_source(request())
    async with source:
        task = asyncio.create_task(sdk.analyze_structure(source))
        try:
            async with asyncio.timeout(3):
                await entered.wait()
                with pytest.raises(StructuraGuardError, match="SDK_RUN_BUSY"):
                    await sdk.analyze_structure(source)
        finally:
            release.set()
            analysis = await task
        assert analysis.plan
        assert not source._run.result.errors


@pytest.mark.anyio
async def test_one_facade_has_independent_concurrent_runs() -> None:
    sdk = engine(FakeDatabase())
    first, second = await asyncio.gather(
        sdk.ingest(request(), dry_run=True), sdk.ingest(request(), dry_run=True)
    )
    assert first.status is second.status is S.COMPLETED
    assert first.run_id != second.run_id
    assert first.source_fingerprint == second.source_fingerprint
    assert first.normalized_fingerprint != second.normalized_fingerprint
    assert {event.run_id for event in first.audit_events} == {first.run_id}
    assert {event.run_id for event in second.audit_events} == {second.run_id}


@pytest.mark.parametrize(
    "terminal",
    (
        S.COMPLETED,
        S.COMPLETED_WITH_WARNINGS,
        S.NEEDS_REVIEW,
        S.REJECTED_SECURITY,
        S.ROLLED_BACK,
        S.FAILED,
        S.CANCELLED,
    ),
)
@pytest.mark.parametrize("following", tuple(S))
def test_terminal_state_has_no_outgoing_transition(terminal: S, following: S) -> None:
    with pytest.raises(StructuraGuardError, match="SDK_INVALID_TRANSITION"):
        check_transition(terminal, following)


@pytest.mark.parametrize(
    "current",
    (
        S.CREATED,
        S.SOURCE_PROBING,
        S.TECHNICAL_PARSING,
        S.STRUCTURE_PROFILING,
        S.STRUCTURE_ANALYZING,
        S.PARSE_PLAN_CREATED,
        S.PARSE_PLAN_VALIDATING,
        S.SEMANTIC_PARSING,
        S.NORMALIZED_DATA_PROFILING,
        S.DATABASE_INSPECTING,
        S.MAPPING,
        S.MAPPING_PLAN_CREATED,
        S.MAPPING_PLAN_VALIDATING,
        S.NORMALIZING,
        S.VALIDATING,
    ),
)
def test_loading_cannot_skip_required_gates(current: S) -> None:
    with pytest.raises(StructuraGuardError, match="SDK_INVALID_TRANSITION"):
        check_transition(current, S.LOADING)
