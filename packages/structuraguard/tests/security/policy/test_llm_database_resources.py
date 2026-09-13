"""Общий run budget действует на реальные adapter boundaries, без внешнего LLM."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from tests.fakes.llm import fixed_clock
from tests.unit.llm.test_router import Clock, Deployment, approved_request, router_for

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.llm import LLMRoutingMode
from structuraguard.contracts.reports import LLMRequest, LLMResponse
from structuraguard.contracts.security import Resource, SecurityLimits, SecurityPolicy
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.llm import PolicyAwareLLMRouter
from structuraguard.security import SecuritySession


def run_for(**limits: int) -> SecuritySession:
    return SecuritySession(
        SecurityPolicy(limits=SecurityLimits(**limits)),
        run_id=UUID(int=2),
        clock=fixed_clock,
    )


@pytest.mark.anyio
@pytest.mark.parametrize("cap", ["calls", "tokens"])
async def test_llm_cumulative_limit_across_routers(cap: str) -> None:
    run = run_for(
        **({"max_llm_calls": 1} if cap == "calls" else {"max_llm_tokens": 120})
    )
    providers = (Deployment("first"), Deployment("second"))
    for index, provider in enumerate(providers):
        router = PolicyAwareLLMRouter(
            policy=router_for((provider,)).policy,
            providers=(provider,),
            run_id="run-1",
            clock=fixed_clock,
            resources=run,
        )
        request = approved_request(router)
        if index == 0:
            await router.generate_structured(request)
        else:
            with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
                await router.generate_structured(request)
            assert provider.fake.call_count == 0
    assert run.used(Resource.LLM_CALLS) == 1
    assert run.used(Resource.LLM_TOKENS) == 120
    assert run.events[0].resource is (
        Resource.LLM_CALLS if cap == "calls" else Resource.LLM_TOKENS
    )


@pytest.mark.anyio
async def test_fallback_reserves_again_and_cannot_exceed_global_budget() -> None:
    providers = (Deployment("primary", failure=True), Deployment("fallback"))
    run = run_for(max_llm_calls=1)
    router = PolicyAwareLLMRouter(
        policy=router_for(providers, mode=LLMRoutingMode.FALLBACK).policy,
        providers=providers,
        run_id="run-1",
        resources=run,
        clock=fixed_clock,
    )
    with pytest.raises(SecurityPolicyError):
        await router.generate_structured(approved_request(router))
    assert providers[0].fake.call_count == 1 and providers[1].fake.call_count == 0
    assert len(router.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("resource", [Resource.PARSER_TIME_MS, Resource.LLM_TIME_MS])
@pytest.mark.parametrize("elapsed", [9, 10, 11])
async def test_stage_deadline_boundary_with_controlled_clock(
    resource: Resource, elapsed: int
) -> None:
    clock = Clock()
    run = SecuritySession(
        SecurityPolicy(
            limits=SecurityLimits(max_parser_time_ms=10, max_llm_time_ms=10)
        ),
        run_id=UUID(int=3),
        monotonic=clock,
        clock=lambda: datetime(2026, 9, 13, tzinfo=UTC),
    )

    async def operation() -> int:
        clock.seconds = elapsed / 1000
        return 42

    if elapsed < 10:
        assert await run.call(operation, resource=resource) == 42
    else:
        with pytest.raises(SecurityPolicyError, match="PROCESSING_TIMEOUT"):
            await run.call(operation, resource=resource)
        assert run.events[0].resource is resource


@pytest.mark.anyio
async def test_aggregate_deadline_is_not_reset_at_stage_change() -> None:
    clock = Clock()
    run = SecuritySession(
        SecurityPolicy(limits=SecurityLimits(max_processing_time_ms=10)),
        run_id=UUID(int=3),
        monotonic=clock,
    )
    run.remaining_seconds(Resource.PARSER_TIME_MS)
    clock.seconds = 0.011
    with pytest.raises(SecurityPolicyError):
        run.reserve_llm(1)
    assert run.used(Resource.LLM_CALLS) == 0
    assert run.events[0].resource is Resource.PROCESSING_TIME_MS


@pytest.mark.anyio
async def test_database_exact_query_count_then_next_adapter_is_blocked(
    tmp_path: Path,
) -> None:
    path = tmp_path / "source.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE data(id INTEGER PRIMARY KEY)")
    target = SQLiteTarget(path=path, target_id="source", include_tables=("data",))
    request = DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )
    baseline = run_for()
    await SQLiteDatabaseAdapter(target, resources=baseline).inspect_metadata(request)
    count = baseline.used(Resource.DB_QUERIES)
    assert count > 1
    run = run_for(max_db_queries=count)
    await SQLiteDatabaseAdapter(target, resources=run).inspect_metadata(request)
    assert run.used(Resource.DB_QUERIES) == count
    with pytest.raises(SecurityPolicyError):
        await SQLiteDatabaseAdapter(target, resources=run).inspect_metadata(request)
    assert run.used(Resource.DB_QUERIES) == count
    assert run.events[0].resource is Resource.DB_QUERIES
    assert str(path) not in run.events[0].canonical_json()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM data").fetchone() == (0,)


@pytest.mark.anyio
async def test_cancelled_llm_does_not_restore_reserved_budget() -> None:
    entered, cleaned = asyncio.Event(), asyncio.Event()

    class BlockingDeployment(Deployment):
        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return await super().generate_structured(request)

    run = run_for()
    provider = BlockingDeployment("local")
    router = PolicyAwareLLMRouter(
        policy=router_for((provider,)).policy,
        providers=(provider,),
        run_id="run-1",
        resources=run,
        clock=fixed_clock,
    )
    task = asyncio.create_task(router.generate_structured(approved_request(router)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set() and provider.fake.call_count == 0
    assert run.used(Resource.LLM_TOKENS) == 120
    assert router.calls[0].outcome == "cancelled"
    assert run.events[0].outcome == "cancelled"


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [0, 1])
async def test_llm_token_allowance_exact_and_one_over(extra: int) -> None:
    run = run_for(max_llm_tokens=120 - extra)
    provider = Deployment("local")
    router = PolicyAwareLLMRouter(
        policy=router_for((provider,)).policy,
        providers=(provider,),
        run_id="run-1",
        resources=run,
        clock=fixed_clock,
    )
    request = approved_request(router)
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await router.generate_structured(request)
        assert provider.fake.call_count == 0
        assert run.used(Resource.LLM_TOKENS) == 0
        assert run.used(Resource.LLM_CALLS) == 0
        assert run.events[0].observed == 120 and run.events[0].limit == 119
    else:
        await router.generate_structured(request)
        assert run.used(Resource.LLM_TOKENS) == 120


@pytest.mark.anyio
async def test_constraint_reader_cannot_bypass_shared_budget(tmp_path: Path) -> None:
    from tests.fakes.mapping import refs
    from tests.security.validation.test_constraint_reader import fixture

    from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
    from structuraguard.database.constraint_reader import DatabaseConstraintReader

    target, catalog, request = await fixture(tmp_path)
    run = run_for(max_db_queries=1)
    reader = DatabaseConstraintReader(
        target, policy=ConstraintReadPolicy(allow_columns=refs(catalog)), resources=run
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await reader.read(request, catalog=catalog)
    assert run.used(Resource.DB_QUERIES) == 1
    assert run.events[0].resource is Resource.DB_QUERIES
    assert "DROP TABLE" not in run.events[0].canonical_json()
