"""PostgreSQL query budget откатывает уже выполненный DML в обеих версиях PG."""

import asyncio
from collections.abc import Callable
from typing import Literal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.staging import StagingClock
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_loader import (
    contents,
    policy_for,
    writer,
)
from tests.unit.llm.test_router import Clock

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.loading import DryRunExecutionPlan, PostgreSQLLoadPolicy
from structuraguard.contracts.security import Resource, SecurityLimits, SecurityPolicy
from structuraguard.contracts.staging import StagingRetentionPolicy, StagingRunStatus
from structuraguard.database import _load_transaction
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.target import PostgreSQLTarget
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.loading.projection import Prepared
from structuraguard.security import SecuritySession
from structuraguard.stores import MemoryStagingStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize(
    "mode", ["success", "limit", "cancel", "deadline", "late_commit"]
)
async def test_query_limit_after_dml_rolls_back_target_and_staging(
    dry_db: Database,
    pg_dsn: str,
    mode: Literal["success", "limit", "cancel", "deadline", "late_commit"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incoming, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=7), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    before = await contents(dry_db, "parents")
    ticks = Clock()
    entered = asyncio.Event()
    run = SecuritySession(
        SecurityPolicy(limits=SecurityLimits(max_db_queries=10000)),
        run_id=UUID(int=1),
        monotonic=ticks,
    )
    original = _load_transaction._write
    wrote = False

    async def write(
        connection: AsyncConnection,
        target: PostgreSQLTarget,
        prepared: Prepared,
        plan: DryRunExecutionPlan,
        catalog: DatabaseCatalog,
        policy: PostgreSQLLoadPolicy,
        check_deadline: Callable[[], None],
    ) -> DryRunExecutionPlan:
        nonlocal wrote
        result = await original(
            connection, target, prepared, plan, catalog, policy, check_deadline
        )
        wrote = True
        if mode == "limit":
            # Довести общий бюджет ровно до N после реального INSERT;
            # следующий metadata statement должен быть N+1 и не исполняться.
            run.reserve(
                Resource.DB_QUERIES,
                run.limits.max_db_queries - run.used(Resource.DB_QUERIES),
            )
        elif mode == "deadline":
            ticks.seconds = 301
        elif mode == "cancel":
            entered.set()
            await asyncio.Event().wait()
        return result

    if mode == "late_commit":
        original_commit = AsyncConnection.commit

        async def commit(connection: AsyncConnection) -> None:
            await original_commit(connection)
            ticks.seconds = 301

        monkeypatch.setattr(AsyncConnection, "commit", commit)

    monkeypatch.setattr(_load_transaction, "_write", write)
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(incoming, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=run.load_policy(policy_for(dry_db, incoming, preflight)),
        staging=store,
        clock=clock,
        resources=run,
    )
    if mode in {"limit", "deadline", "cancel"}:
        if mode == "cancel":
            task = asyncio.create_task(loader.execute(request))
            await asyncio.wait_for(entered.wait(), timeout=10)
            task.cancel("secret-canary")
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await task
            assert cancelled.value.args == ()
        else:
            code = (
                "SECURITY_LIMIT_EXCEEDED" if mode == "limit" else "PROCESSING_TIMEOUT"
            )
            with pytest.raises(SecurityPolicyError, match=code):
                await loader.execute(request)
        assert wrote
        assert await contents(dry_db, "parents") == before
        assert (await store.get_run(request.staging_context)).status is (
            StagingRunStatus.CANCELLED
            if mode == "cancel"
            else StagingRunStatus.ROLLED_BACK
        )
        if mode == "limit":
            assert run.events[0].resource is Resource.DB_QUERIES
        elif mode == "deadline":
            assert run.events[0].code == "PROCESSING_TIMEOUT"
        else:
            assert run.events[0].outcome == "cancelled"
        assert "secret" not in run.events[0].canonical_json()
    else:
        result = await loader.execute(request)
        assert wrote and result.inserted == 1
        assert await contents(dry_db, "parents") == (*before, (7, 20))
        assert run.used(Resource.DB_QUERIES) > 1 and not run.events


async def test_dry_run_uses_shared_query_budget(dry_db: Database) -> None:
    from structuraguard.database.dry_run import PostgreSQLDryRunPlanner

    incoming, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=7), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    before = await dry_db.snapshot()
    run = SecuritySession(
        SecurityPolicy(limits=SecurityLimits(max_db_queries=1)), run_id=UUID(int=1)
    )
    planner = PostgreSQLDryRunPlanner(dry_db.target, policy=preflight, resources=run)
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await planner.plan(incoming)
    assert run.used(Resource.DB_QUERIES) == 1
    assert run.events[0].resource is Resource.DB_QUERIES
    assert await dry_db.snapshot() == before
