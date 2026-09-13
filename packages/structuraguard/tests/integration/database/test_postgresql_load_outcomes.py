"""M13-D: атомарный ledger, quarantine, rollback, concurrency и commit recovery."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass
from typing import Literal

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.mapping_validation import replan
from tests.fakes.staging import StagingClock
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_loader import (
    contents,
    policy_for,
    writer,
)

from structuraguard.contracts.common import IntegerScalar, NormalizedScalar
from structuraguard.contracts.database import DatabaseCatalog, StagingContext
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
    LoadLedgerPolicy,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.staging import (
    StagingRetentionPolicy,
    StagingRun,
    StagingRunStatus,
)
from structuraguard.database import _load_transaction
from structuraguard.database._load_ledger import Ledger
from structuraguard.database._load_queries import WriteBatch, statements
from structuraguard.database.ledger import bootstrap_postgresql_loader
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.exceptions import LoadError
from structuraguard.loading.projection import Prepared
from structuraguard.stores import MemoryStagingStore, PostgreSQLStagingTarget

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


def after[T, **P](
    function: Callable[P, Awaitable[T]],
    hook: Callable[[], Awaitable[None]],
) -> Callable[P, Awaitable[T]]:
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        result = await function(*args, **kwargs)
        await hook()
        return result

    return wrapped


@dataclass
class LedgerDatabase:
    db: Database
    policy: LoadLedgerPolicy

    async def rows(self, name: str) -> tuple[tuple[object, ...], ...]:
        async with self.db.engine.connect() as connection:
            return tuple(
                tuple(row)
                for row in (
                    await connection.exec_driver_sql(
                        f'SELECT xmin::text,ctid::text,t.* FROM "{self.policy.schema_name}"."{name}" t ORDER BY 1,2'
                    )
                ).all()
            )

    async def unchanged(self) -> tuple[object, ...]:
        return (
            await self.db.snapshot(),
            await self.rows("execution_commits"),
            await self.rows("execution_audit"),
            await self.rows("execution_quarantine"),
        )


@pytest.fixture
async def ledger_db(dry_db: Database, pg_dsn: str) -> AsyncIterator[LedgerDatabase]:
    policy = LoadLedgerPolicy(
        namespace="tenant", schema_name="sg_staging_load_" + dry_db.schema[4:]
    )
    admin = PostgreSQLStagingTarget(
        dsn=SecretStr(pg_dsn),
        principal="test",
        inspector_principal="inspector",
        target_id="main",
        namespace=policy.namespace,
        schema_name=policy.schema_name,
        purpose="bootstrap",
    )
    await bootstrap_postgresql_loader(admin)
    async with dry_db.engine.begin() as connection:
        await connection.exec_driver_sql(
            f'GRANT USAGE ON SCHEMA "{policy.schema_name}" TO dry_writer'
        )
        await connection.exec_driver_sql(
            f'GRANT SELECT ON ALL TABLES IN SCHEMA "{policy.schema_name}" TO dry_writer'
        )
        for name in ("execution_commits", "execution_audit", "execution_quarantine"):
            await connection.exec_driver_sql(
                f'GRANT INSERT ON "{policy.schema_name}"."{name}" TO dry_writer'
            )
    try:
        yield LedgerDatabase(dry_db, policy)
    finally:
        async with dry_db.engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA "{policy.schema_name}" CASCADE'
            )


@dataclass
class Case:
    loader: PostgreSQLLoader
    request: LoadRequest
    store: MemoryStagingStore
    clock: StagingClock


async def load_case(
    ledger: LedgerDatabase,
    dsn: str,
    snapshot: DryRunRequest,
    preflight: DryRunPolicy,
    *,
    key: str = "secret-idempotency-key-55",
) -> Case:
    policy = policy_for(
        ledger.db,
        snapshot,
        preflight.model_copy(update={"error_policy": "atomic"}),
        size=1,
    )
    policy = policy.model_copy(update={"preflight": preflight, "ledger": ledger.policy})
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = (await sealed_input(snapshot, store, clock)).model_copy(
        update={"idempotency_key": key}
    )
    return Case(
        PostgreSQLLoader(
            writer(ledger.db, dsn), policy=policy, staging=store, clock=clock
        ),
        request,
        store,
        clock,
    )


async def parent_case(ledger: LedgerDatabase, dsn: str) -> Case:
    return await load_case(
        ledger,
        dsn,
        *(
            await dry_run_case(
                ledger.db.catalog,
                [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
                table_name="parents",
            )
        ),
    )


async def assert_empty_ledger(ledger: LedgerDatabase) -> None:
    for name in ("execution_commits", "execution_audit", "execution_quarantine"):
        assert await ledger.rows(name) == ()


async def test_success_duplicate_request_and_redacted_audit(
    ledger_db: LedgerDatabase, pg_dsn: str
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    before = await ledger_db.unchanged()
    assert (await case.loader.dry_run(case.request.snapshot)).ready
    assert await ledger_db.unchanged() == before
    result = await case.loader.execute(case.request)
    assert (result.inserted, result.loaded_records, result.rejected_records) == (
        1,
        1,
        0,
    )
    assert not result.replayed
    assert len(await ledger_db.rows("execution_commits")) == 1
    audit = repr(await ledger_db.rows("execution_audit"))
    assert "secret-idempotency-key-55" not in audit
    assert "dry-writer-canary-77" not in audit
    assert "source_refs" not in audit and "parameters" not in audit
    before = await ledger_db.unchanged()
    replay = await case.loader.execute(case.request)
    assert replay.replayed and replay.run_id == result.run_id
    assert replay.safe_summary()["inserted"] == 0
    assert await ledger_db.unchanged() == before
    fresh = (
        await sealed_input(
            case.request.snapshot, case.store, case.clock, run_id="fresh"
        )
    ).model_copy(update={"idempotency_key": case.request.idempotency_key})
    assert (await case.loader.execute(fresh)).replayed
    assert await ledger_db.unchanged() == before


async def test_runtime_finalize_failure_preserves_commit_and_allows_ledger_recovery(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    original = case.store.transition

    async def fail(
        context: StagingContext, *, expected_revision: int, status: StagingRunStatus
    ) -> StagingRun:
        if status is StagingRunStatus.COMMITTED:
            raise RuntimeError("staging-finalize-secret-canary")
        return await original(
            context, expected_revision=expected_revision, status=status
        )

    with monkeypatch.context() as fault:
        fault.setattr(case.store, "transition", fail)
        result = await case.loader.execute(case.request)
    assert result.transaction_outcome == "committed" and not result.replayed
    assert result.inserted == result.loaded_records == 1
    assert result.warnings == ("LOAD_STAGING_FINALIZE_FAILED",)
    assert await contents(ledger_db.db, "parents") == ((1, 10), (2, 20))
    assert len(await ledger_db.rows("execution_commits")) == 1
    assert len(await ledger_db.rows("execution_audit")) == 1
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.EXECUTING
    before = await ledger_db.unchanged()
    replay = await case.loader.execute(case.request)
    assert replay.replayed and replay.run_id == result.run_id
    assert replay.safe_summary()["inserted"] == 0
    assert replay.warnings == ()
    assert await ledger_db.unchanged() == before
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.COMMITTED
    assert "staging-finalize-secret-canary" not in (
        result.model_dump_json() + replay.model_dump_json() + repr(before) + caplog.text
    )


async def test_runtime_staging_read_failure_preserves_committed_ledger_replay(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    original = await case.loader.execute(case.request)
    before = await ledger_db.unchanged()

    async def fail(context: StagingContext) -> StagingRun:
        raise RuntimeError("staging-recovery-secret-canary")

    with monkeypatch.context() as fault:
        fault.setattr(case.store, "get_run", fail)
        result = await case.loader.execute(case.request)
    assert result.transaction_outcome == "committed" and result.replayed
    assert result.run_id == original.run_id
    assert result.inserted == original.inserted == 1
    assert result.safe_summary()["inserted"] == result.safe_summary()["updated"] == 0
    assert result.warnings == ("LOAD_STAGING_FINALIZE_FAILED",)
    assert await ledger_db.unchanged() == before
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.COMMITTED
    assert "staging-recovery-secret-canary" not in (
        result.model_dump_json() + repr(before) + caplog.text
    )


@pytest.mark.parametrize("kind", ["validation", "fk"])
async def test_validation_and_fk_failure_roll_back_everything(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    kind: str,
) -> None:
    values: dict[str, NormalizedScalar] = {
        "id": IntegerScalar(value=1),
        "amount": IntegerScalar(value=20),
    }
    table = "parents"
    if kind == "fk":
        values.update(id=IntegerScalar(value=2), parent=IntegerScalar(value=404))
        table = "children"
    case = await load_case(
        ledger_db,
        pg_dsn,
        *(await dry_run_case(ledger_db.db.catalog, [values], table_name=table)),
    )
    before = await ledger_db.db.snapshot()
    with pytest.raises(
        LoadError,
        match="LOAD_DUPLICATE_KEY" if kind == "validation" else "LOAD_FOREIGN_KEY",
    ):
        await case.loader.execute(case.request)
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.ROLLED_BACK


async def test_exception_in_middle_of_batch_rolls_back_target_and_marker(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    original = statements

    def fail_after_write(
        prepared: Prepared,
        plan: DryRunExecutionPlan,
        catalog: DatabaseCatalog,
        policy: PostgreSQLLoadPolicy,
        *,
        unit_ids: frozenset[str] | None = None,
    ) -> Iterator[WriteBatch]:
        for batch in original(prepared, plan, catalog, policy, unit_ids=unit_ids):
            yield batch
            raise RuntimeError("restricted-internal-error-38")

    monkeypatch.setattr(_load_transaction, "statements", fail_after_write)
    before = await ledger_db.db.snapshot()
    with pytest.raises(LoadError, match="LOAD_DATABASE_FAILED") as caught:
        await case.loader.execute(case.request)
    assert "restricted-internal-error-38" not in str(caught.value)
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)


async def test_cancellation_after_dml_rolls_back_target_and_ledger(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    entered, hold = asyncio.Event(), asyncio.Event()
    original = _load_transaction._write

    async def pause() -> None:
        entered.set()
        await hold.wait()

    monkeypatch.setattr(_load_transaction, "_write", after(original, pause))
    before = await ledger_db.db.snapshot()
    task = asyncio.create_task(case.loader.execute(case.request))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.CANCELLED


@pytest.mark.parametrize("rollback_first", [False, True])
async def test_concurrent_same_key_waits_for_commit_or_rollback(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    rollback_first: bool,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    fresh = (
        await sealed_input(
            case.request.snapshot, case.store, case.clock, run_id="concurrent"
        )
    ).model_copy(update={"idempotency_key": case.request.idempotency_key})
    locked, arrived, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_claim = Ledger.claim
    calls = 0

    async def claim(
        self: Ledger, connection: AsyncConnection
    ) -> PostgreSQLLoadResult | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            arrived.set()
        result = await original_claim(self, connection)
        if calls == 1:
            locked.set()
            await release.wait()
        return result

    monkeypatch.setattr(Ledger, "claim", claim)
    original_write = _load_transaction._write
    writes = 0

    async def write() -> None:
        nonlocal writes
        writes += 1
        if rollback_first and writes == 1:
            raise RuntimeError("first-attempt-failure")

    monkeypatch.setattr(_load_transaction, "_write", after(original_write, write))
    first = asyncio.create_task(case.loader.execute(case.request))
    await asyncio.wait_for(locked.wait(), timeout=10)
    second = asyncio.create_task(case.loader.execute(fresh))
    await asyncio.wait_for(arrived.wait(), timeout=10)
    try:
        # Подтвердить реальное ожидание DB lock, не только запуск coroutine.
        from sqlalchemy import text

        async with asyncio.timeout(5), ledger_db.db.engine.connect() as connection:
            for _ in range(1000):
                waiting = (
                    await connection.execute(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE locktype='advisory' "
                            "AND NOT granted AND database=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database()))"
                        )
                    )
                ).scalar_one()
                if waiting:
                    break
            else:
                pytest.fail("Concurrent request не ожидал idempotency lock")
        assert not second.done()
    finally:
        release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if rollback_first:
        assert isinstance(results[0], LoadError)
        assert isinstance(results[1], PostgreSQLLoadResult) and not results[1].replayed
    else:
        assert all(isinstance(r, PostgreSQLLoadResult) for r in results)
        assert isinstance(results[1], PostgreSQLLoadResult) and results[1].replayed
    assert await contents(ledger_db.db, "parents") == ((1, 10), (2, 20))
    assert len(await ledger_db.rows("execution_commits")) == 1
    assert len(await ledger_db.rows("execution_audit")) == 1


async def test_schema_drift_after_dml_before_commit_rolls_back_ledger(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    original = Ledger.commit

    async def drift(
        self: Ledger,
        connection: AsyncConnection,
        result: PostgreSQLLoadResult,
        plan: DryRunExecutionPlan,
    ) -> None:
        await original(self, connection, result, plan)
        await ledger_db.db.sql("COMMENT ON SCHEMA $schema IS 'changed-before-commit'")

    monkeypatch.setattr(Ledger, "commit", drift)
    before = await ledger_db.db.snapshot()
    with pytest.raises(LoadError, match="DATABASE_SCHEMA_DRIFT"):
        await case.loader.execute(case.request)
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)


async def test_lost_commit_response_recovers_same_key_without_writes(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    original = AsyncConnection.commit

    async def lose(self: AsyncConnection) -> None:
        await original(self)
        raise OSError("commit-secret-ack-39")

    monkeypatch.setattr(AsyncConnection, "commit", lose)
    with pytest.raises(LoadError, match="LOAD_OUTCOME_UNKNOWN"):
        await case.loader.execute(case.request)
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.UNKNOWN
    monkeypatch.setattr(AsyncConnection, "commit", original)
    before = await ledger_db.unchanged()
    result = await case.loader.execute(case.request)
    assert result.replayed and result.safe_summary()["inserted"] == 0
    assert await ledger_db.unchanged() == before
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.COMMITTED


@pytest.mark.parametrize("mode", ["atomic", "quarantine_invalid"])
async def test_fk_validation_quarantine_is_explicit_and_persistent(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    mode: Literal["atomic", "quarantine_invalid"],
) -> None:
    request, preflight = await dry_run_case(
        ledger_db.db.catalog,
        [
            {
                "id": IntegerScalar(value=2),
                "parent": IntegerScalar(value=1),
                "amount": IntegerScalar(value=20),
            },
            {
                "id": IntegerScalar(value=3),
                "parent": IntegerScalar(value=404),
                "amount": IntegerScalar(value=30),
            },
        ],
        table_name="children",
        error_policy=mode,
    )
    case = await load_case(ledger_db, pg_dsn, request, preflight)
    if mode == "atomic":
        with pytest.raises(LoadError, match="LOAD_FOREIGN_KEY"):
            await case.loader.execute(case.request)
        assert await contents(ledger_db.db, "children") == ()
        await assert_empty_ledger(ledger_db)
    else:
        result = await case.loader.execute(case.request)
        assert (
            result.inserted,
            result.quarantined,
            result.loaded_records,
            result.rejected_records,
        ) == (1, 1, 1, 1)
        assert await contents(ledger_db.db, "children") == ((2, 1, 20),)
        quarantined = await ledger_db.rows("execution_quarantine")
        assert len(quarantined) == 1 and "DB_FOREIGN_KEY" in repr(quarantined)
        assert (
            await case.store.get_run(case.request.staging_context)
        ).status is StagingRunStatus.QUARANTINED
        before = await ledger_db.unchanged()
        assert (await case.loader.execute(case.request)).replayed
        assert await ledger_db.unchanged() == before


async def test_late_fk_failure_rolls_back_parent_group_and_continues_independent_group(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import literal
    from sqlalchemy.dialects.postgresql import insert

    snapshot, preflight = await dry_run_case(
        ledger_db.db.catalog,
        [
            {
                "parent_id": IntegerScalar(value=i),
                "parent_amount": IntegerScalar(value=20),
                "child_id": IntegerScalar(value=i),
                "child_parent": IntegerScalar(value=i),
                "child_amount": IntegerScalar(value=30),
            }
            for i in (8, 9)
        ],
        table_name="parents",
        error_policy="quarantine_invalid",
        targets={
            "parent_id": ("parents", "id"),
            "parent_amount": ("parents", "amount"),
            "child_id": ("children", "id"),
            "child_parent": ("children", "parent"),
            "child_amount": ("children", "amount"),
        },
    )
    case = await load_case(ledger_db, pg_dsn, snapshot, preflight)
    injected = False

    def late_fk(
        prepared: Prepared,
        plan: DryRunExecutionPlan,
        catalog: DatabaseCatalog,
        policy: PostgreSQLLoadPolicy,
        *,
        unit_ids: frozenset[str] | None = None,
    ) -> Iterator[WriteBatch]:
        nonlocal injected
        child_column = next(
            c.column_id
            for schema in catalog.schemas
            for table in schema.tables
            if table.name == "children"
            for c in table.columns
            if c.name == "id"
        )
        rejected_units = {
            r.record_id
            for r in prepared.data.records
            if any(
                c.field_id == child_column and c.value == IntegerScalar(value=8)
                for c in r.values
            )
        }
        for batch in statements(prepared, plan, catalog, policy, unit_ids=unit_ids):
            if not injected and rejected_units.intersection(batch.unit_ids):
                injected = True
                yield WriteBatch(
                    batch.unit_ids,
                    insert(batch.statement.table)
                    .values(id=88, parent=404, amount=20)
                    .returning(literal(1)),
                )
            else:
                yield batch

    monkeypatch.setattr(_load_transaction, "statements", late_fk)
    result = await case.loader.execute(case.request)
    assert (
        result.inserted,
        result.quarantined,
        result.loaded_records,
        result.rejected_records,
    ) == (2, 2, 1, 1)
    assert await contents(ledger_db.db, "parents") == ((1, 10), (9, 20))
    assert await contents(ledger_db.db, "children") == ((9, 9, 30),)
    assert len(await ledger_db.rows("execution_quarantine")) == 2
    assert "LOAD_FOREIGN_KEY" in repr(await ledger_db.rows("execution_quarantine"))


async def test_audit_permission_failure_after_dml_rolls_back_marker_and_target(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)

    async def revoke_audit() -> None:
        async with ledger_db.db.engine.begin() as connection:
            await connection.exec_driver_sql(
                f'REVOKE INSERT ON "{ledger_db.policy.schema_name}".execution_audit FROM dry_writer'
            )

    monkeypatch.setattr(
        _load_transaction, "_write", after(_load_transaction._write, revoke_audit)
    )
    before = await ledger_db.db.snapshot()
    with pytest.raises(LoadError, match="LOAD_PERMISSION_DENIED"):
        await case.loader.execute(case.request)
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)


@pytest.mark.parametrize("changed", ["plan", "content", "policy"])
async def test_same_key_with_different_binding_is_rejected_before_staging_claim(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    changed: str,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    await case.loader.execute(case.request)
    snapshot = case.request.snapshot
    preflight = case.loader._policy.preflight
    if changed == "plan":
        snapshot = snapshot.model_copy(
            update={"mapping": replan(snapshot.mapping, plan_id="different-plan")}
        )
    elif changed == "content":
        snapshot, preflight = await dry_run_case(
            ledger_db.db.catalog,
            [{"id": IntegerScalar(value=3), "amount": IntegerScalar(value=30)}],
            table_name="parents",
        )
    else:
        preflight = preflight.model_copy(update={"error_policy": "quarantine_invalid"})
    other = await load_case(ledger_db, pg_dsn, snapshot, preflight)
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="IDEMPOTENCY_KEY_CONFLICT"):
        await other.loader.execute(other.request)
    assert await ledger_db.unchanged() == before
    assert (
        await other.store.get_run(other.request.staging_context)
    ).status is StagingRunStatus.SEALED


async def test_all_invalid_quarantine_commits_zero_target_rows(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
) -> None:
    case = await load_case(
        ledger_db,
        pg_dsn,
        *(
            await dry_run_case(
                ledger_db.db.catalog,
                [
                    {
                        "id": IntegerScalar(value=8),
                        "parent": IntegerScalar(value=404),
                        "amount": IntegerScalar(value=30),
                    }
                ],
                table_name="children",
                error_policy="quarantine_invalid",
            )
        ),
    )
    before = await ledger_db.db.snapshot()
    result = await case.loader.execute(case.request)
    assert (
        result.inserted,
        result.loaded_records,
        result.quarantined,
        result.rejected_records,
    ) == (0, 0, 1, 1)
    assert await ledger_db.db.snapshot() == before
    assert len(await ledger_db.rows("execution_commits")) == 1
    assert len(await ledger_db.rows("execution_quarantine")) == 1


async def test_missing_ledger_is_never_bootstrapped_by_load(
    dry_db: Database,
    pg_dsn: str,
) -> None:
    from sqlalchemy import text

    ledger = LedgerDatabase(
        dry_db,
        LoadLedgerPolicy(namespace="tenant", schema_name="sg_staging_load_missing"),
    )
    case = await parent_case(ledger, pg_dsn)
    before = await dry_db.snapshot()
    with pytest.raises(LoadError):
        await case.loader.execute(case.request)
    assert await dry_db.snapshot() == before
    async with dry_db.engine.connect() as connection:
        assert not (
            await connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=:schema)"
                ),
                {"schema": ledger.policy.schema_name},
            )
        ).scalar_one()


async def test_quarantine_does_not_bypass_global_validation_gate(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
) -> None:
    from tests.integration.database.test_postgresql_loader import refresh

    await ledger_db.db.sql("ALTER TABLE $schema.parents ADD CHECK (amount > 0)")
    await refresh(ledger_db.db)
    snapshot, preflight = await dry_run_case(
        ledger_db.db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
        error_policy="quarantine_invalid",
    )
    case = await load_case(ledger_db, pg_dsn, snapshot, preflight)
    before = await ledger_db.db.snapshot()
    with pytest.raises(LoadError, match="LOAD_VALIDATION_REJECTED"):
        await case.loader.execute(case.request)
    assert await ledger_db.db.snapshot() == before
    await assert_empty_ledger(ledger_db)


async def test_ledger_survives_staging_expiry_and_cleanup(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
) -> None:
    from datetime import timedelta

    case = await parent_case(ledger_db, pg_dsn)
    result = await case.loader.execute(case.request)
    case.clock.now += timedelta(days=2)
    assert (await case.store.cleanup(case.request.staging_context)).purged
    before = await ledger_db.unchanged()
    replay = await case.loader.execute(case.request)
    assert (
        replay.replayed
        and replay.execution_plan_fingerprint == result.execution_plan_fingerprint
    )
    assert await ledger_db.unchanged() == before


async def test_replay_rejects_new_snapshot_with_old_mapping(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    await case.loader.execute(case.request)
    other, _ = await dry_run_case(
        ledger_db.db.catalog,
        [{"id": IntegerScalar(value=3), "amount": IntegerScalar(value=30)}],
        table_name="parents",
    )
    request = case.request.model_copy(
        update={
            "snapshot": case.request.snapshot.model_copy(
                update={"batches": other.batches}
            )
        }
    )
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="LOAD_INPUT_BINDING_MISMATCH"):
        await case.loader.execute(request)
    assert await ledger_db.unchanged() == before


@pytest.mark.parametrize(
    "grant",
    [
        "UPDATE(payload,fingerprint,binding) ON execution_commits",
        "INSERT(version,fingerprint) ON schema_info",
    ],
)
async def test_column_grants_cannot_weaken_append_only_ledger(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    grant: str,
) -> None:
    rights, table = grant.split(" ON ")
    async with ledger_db.db.engine.begin() as connection:
        await connection.exec_driver_sql(
            f'GRANT {rights} ON "{ledger_db.policy.schema_name}"."{table}" TO dry_writer'
        )
    case = await parent_case(ledger_db, pg_dsn)
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="LOAD_LEDGER_PERMISSION_DENIED"):
        await case.loader.execute(case.request)
    assert await ledger_db.unchanged() == before
