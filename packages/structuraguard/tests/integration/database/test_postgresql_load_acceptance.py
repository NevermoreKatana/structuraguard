"""Пробелы приёмки M13: блокировки, отмена и неопределённый COMMIT."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.fakes.loading import dry_run_case, sealed_input
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_load_outcomes import (
    LedgerDatabase,
    after,
    assert_empty_ledger,
    load_case,
    parent_case,
)
from tests.integration.database.test_postgresql_load_outcomes import (
    ledger_db as ledger_db,
)
from tests.integration.database.test_postgresql_loader import contents, refresh

from structuraguard.contracts.common import (
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    LoadOperation,
)
from structuraguard.contracts.loading import PostgreSQLLoadResult
from structuraguard.contracts.staging import StagingRunStatus
from structuraguard.database import _load_transaction
from structuraguard.database._load_ledger import Ledger
from structuraguard.exceptions import LoadError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_decimal_and_utc_values_survive_real_insert_and_update(
    ledger_db: LedgerDatabase, pg_dsn: str, operation: LoadOperation
) -> None:
    db = ledger_db.db
    await db.sql(
        "CREATE TABLE $schema.typed (id integer PRIMARY KEY, amount numeric(18,6), occurred timestamptz)"
    )
    await db.sql("GRANT SELECT ON $schema.typed TO inspector,dry_writer")
    await db.sql("GRANT INSERT,UPDATE ON $schema.typed TO dry_writer")
    if operation is LoadOperation.UPSERT:
        await db.sql("INSERT INTO $schema.typed VALUES (2,1,'2000-01-01T00:00:00Z')")
    await refresh(db, ("typed",))
    amount = Decimal("123456789012.345678")
    occurred = datetime(2026, 9, 13, 12, 34, 56, 123456, tzinfo=UTC)
    case = await load_case(
        ledger_db,
        pg_dsn,
        *(
            await dry_run_case(
                db.catalog,
                [
                    {
                        "id": IntegerScalar(value=2),
                        "amount": DecimalScalar(value=amount),
                        "occurred": DateTimeScalar(value=occurred),
                    }
                ],
                table_name="typed",
                operation=operation,
                semantic_type="unresolved",
            )
        ),
    )
    result = await case.loader.execute(case.request)
    assert (result.inserted, result.updated) == (
        (1, 0) if operation is LoadOperation.INSERT_ONLY else (0, 1)
    )
    assert await contents(db, "typed") == ((2, amount, occurred),)


async def test_generated_primary_key_is_explicitly_rejected_without_consuming_sequence(
    ledger_db: LedgerDatabase, pg_dsn: str
) -> None:
    db = ledger_db.db
    await db.sql(
        "CREATE TABLE $schema.generated_key (id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY, amount integer)"
    )
    await db.sql("GRANT SELECT ON $schema.generated_key TO inspector,dry_writer")
    await db.sql("GRANT INSERT,UPDATE ON $schema.generated_key TO dry_writer")
    await db.sql("GRANT USAGE ON SEQUENCE $schema.generated_key_id_seq TO dry_writer")
    await refresh(db, ("generated_key",))
    case = await load_case(
        ledger_db,
        pg_dsn,
        *(
            await dry_run_case(
                db.catalog,
                [{"amount": IntegerScalar(value=20)}],
                table_name="generated_key",
            )
        ),
    )
    plan = await case.loader.dry_run(case.request.snapshot)
    assert not plan.ready
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="LOAD_VALIDATION_REJECTED"):
        await case.loader.execute(case.request)
    assert await ledger_db.unchanged() == before
    assert await contents(db, "generated_key") == ()
    async with db.engine.connect() as connection:
        assert (
            await connection.exec_driver_sql(
                f'SELECT last_value,is_called FROM "{db.schema}".generated_key_id_seq'
            )
        ).one() == (1, False)


async def wait_for_lock(db: LedgerDatabase, pid: int) -> None:
    """Ожидать наблюдаемой блокировки конкретного writer backend."""
    async with asyncio.timeout(5), db.db.engine.connect() as connection:
        for _ in range(2000):
            if (
                await connection.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks "
                        "WHERE pid=:pid AND NOT granted)"
                    ),
                    {"pid": pid},
                )
            ).scalar_one():
                return
    pytest.fail("Writer не достиг ожидаемой блокировки")


async def assert_released(db: LedgerDatabase, pid: int) -> None:
    async with asyncio.timeout(5), db.db.engine.connect() as connection:
        for _ in range(2000):
            if not (
                await connection.execute(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_locks WHERE pid=:pid)"
                    ),
                    {"pid": pid},
                )
            ).scalar_one():
                return
    pytest.fail("Writer оставил блокировки после завершения")


@pytest.mark.parametrize("kind", ["fingerprint", "payload", "oversize", "binding"])
async def test_corrupt_ledger_receipt_cannot_authorize_replay_or_leak_payload(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    kind: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    await case.loader.execute(case.request)
    column = "payload" if kind in {"payload", "oversize"} else kind
    value = "receipt-secret-canary-29"
    expected = "LOAD_LEDGER_RECEIPT_INVALID"
    if kind == "payload":
        expected = "LOAD_DATABASE_FAILED"
    elif kind == "binding":
        expected = "IDEMPOTENCY_KEY_CONFLICT"
    elif kind == "oversize":
        value *= ledger_db.policy.max_receipt_bytes // len(value) + 1
    async with ledger_db.db.engine.begin() as connection:
        await connection.execute(
            text(
                f'UPDATE "{ledger_db.policy.schema_name}".execution_commits SET "{column}"=:value'
            ),
            {"value": value},
        )
    before = await ledger_db.unchanged()
    run = await case.store.get_run(case.request.staging_context)
    with pytest.raises(LoadError, match=expected) as caught:
        await case.loader.execute(case.request)
    assert await ledger_db.unchanged() == before
    assert await case.store.get_run(case.request.staging_context) == run
    assert (
        "receipt-secret-canary-29"
        not in str(caught.value) + repr(caught.value) + caplog.text
    )


async def test_cancelled_same_key_waiter_leaves_its_staging_sealed_and_owner_commits(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    fresh = (
        await sealed_input(
            case.request.snapshot, case.store, case.clock, run_id="waiter"
        )
    ).model_copy(update={"idempotency_key": case.request.idempotency_key})
    sealed = await case.store.get_run(fresh.staging_context)
    written, arrived, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    claim = Ledger.claim
    pid = 0

    async def capture(
        self: Ledger, connection: AsyncConnection
    ) -> PostgreSQLLoadResult | None:
        nonlocal pid
        if self.request.staging_context.run_id == "waiter":
            pid = (
                await connection.exec_driver_sql("SELECT pg_backend_pid()")
            ).scalar_one()
            arrived.set()
        return await claim(self, connection)

    async def pause_write() -> None:
        written.set()
        await release.wait()

    monkeypatch.setattr(Ledger, "claim", capture)
    monkeypatch.setattr(
        _load_transaction, "_write", after(_load_transaction._write, pause_write)
    )
    owner = asyncio.create_task(case.loader.execute(case.request))
    waiter: asyncio.Task[PostgreSQLLoadResult] | None = None
    try:
        await asyncio.wait_for(written.wait(), 10)
        waiter = asyncio.create_task(case.loader.execute(fresh))
        await asyncio.wait_for(arrived.wait(), 5)
        await wait_for_lock(ledger_db, pid)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await case.store.get_run(fresh.staging_context) == sealed
        await assert_released(ledger_db, pid)
        release.set()
        assert (await owner).inserted == 1
    finally:
        release.set()
        tasks = [owner] + ([waiter] if waiter is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    before = await ledger_db.unchanged()
    assert (await case.loader.execute(fresh)).replayed
    assert await ledger_db.unchanged() == before
    assert await case.store.get_run(fresh.staging_context) == sealed


@pytest.mark.parametrize("reason", ["cancel", "lock", "statement", "deadline"])
async def test_table_lock_interruption_rolls_back_and_releases_all_locks(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    db = ledger_db.db
    limits = db.target.limits.model_copy(
        update={
            "timeout_seconds": 1.5 if reason == "deadline" else 10.0,
            "lock_timeout_seconds": 0.5 if reason == "lock" else 5.0,
            "statement_timeout_seconds": 0.5 if reason == "statement" else 10.0,
        }
    )
    db.target = db.target.model_copy(update={"limits": limits})
    await refresh(db)
    case = await parent_case(ledger_db, pg_dsn)
    connected = asyncio.Event()
    pid = 0
    claim = Ledger.claim

    async def capture(
        self: Ledger, connection: AsyncConnection
    ) -> PostgreSQLLoadResult | None:
        nonlocal pid
        result = await claim(self, connection)
        pid = (await connection.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one()
        connected.set()
        return result

    monkeypatch.setattr(Ledger, "claim", capture)
    before = await ledger_db.unchanged()
    async with db.engine.begin() as blocker:
        await blocker.exec_driver_sql(
            f'LOCK TABLE "{db.schema}".parents IN ACCESS EXCLUSIVE MODE'
        )
        task = asyncio.create_task(case.loader.execute(case.request))
        try:
            await asyncio.wait_for(connected.wait(), 5)
            await wait_for_lock(ledger_db, pid)
            if reason == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(LoadError, match="PROCESSING_TIMEOUT"):
                    await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert await ledger_db.unchanged() == before
    assert (await case.store.get_run(case.request.staging_context)).status is (
        StagingRunStatus.CANCELLED
        if reason == "cancel"
        else StagingRunStatus.ROLLED_BACK
    )
    await assert_released(ledger_db, pid)
    fresh = (
        await sealed_input(
            case.request.snapshot, case.store, case.clock, run_id="retry"
        )
    ).model_copy(update={"idempotency_key": case.request.idempotency_key})
    assert (await case.loader.execute(fresh)).inserted == 1


async def test_repeated_cancellation_during_rollback_waits_for_cleanup(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    written, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writer_connection: AsyncConnection | None = None
    pid = 0
    claim, close = Ledger.claim, AsyncConnection.close

    async def capture(
        self: Ledger, connection: AsyncConnection
    ) -> PostgreSQLLoadResult | None:
        nonlocal writer_connection, pid
        writer_connection = connection
        pid = (await connection.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one()
        return await claim(self, connection)

    async def pause_write() -> None:
        written.set()
        await asyncio.Event().wait()

    async def pause_close(self: AsyncConnection) -> None:
        if self is writer_connection:
            closing.set()
            await release.wait()
        await close(self)

    monkeypatch.setattr(Ledger, "claim", capture)
    monkeypatch.setattr(AsyncConnection, "close", pause_close)
    monkeypatch.setattr(
        _load_transaction, "_write", after(_load_transaction._write, pause_write)
    )
    before = await ledger_db.unchanged()
    task = asyncio.create_task(case.loader.execute(case.request))
    try:
        await asyncio.wait_for(written.wait(), 10)
        task.cancel()
        await asyncio.wait_for(closing.wait(), 5)
        for _ in range(3):
            task.cancel()
            # DB round trip даёт cancellation дойти до защищённой cleanup task.
            async with ledger_db.db.engine.connect() as observer:
                await observer.exec_driver_sql("SELECT 1")
            assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert await ledger_db.unchanged() == before
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.CANCELLED
    await assert_released(ledger_db, pid)


@pytest.mark.parametrize("phase", ["before_commit", "after_commit"])
async def test_cancellation_inside_commit_is_unknown_until_ledger_reconciliation(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    commit = AsyncConnection.commit

    async def interrupted(self: AsyncConnection) -> None:
        if phase == "after_commit":
            await commit(self)
        raise asyncio.CancelledError

    monkeypatch.setattr(AsyncConnection, "commit", interrupted)
    before = await ledger_db.unchanged()
    with pytest.raises(LoadError, match="LOAD_OUTCOME_UNKNOWN"):
        await case.loader.execute(case.request)
    assert (
        await case.store.get_run(case.request.staging_context)
    ).status is StagingRunStatus.UNKNOWN
    monkeypatch.setattr(AsyncConnection, "commit", commit)
    if phase == "after_commit":
        committed = await ledger_db.unchanged()
        assert (await case.loader.execute(case.request)).replayed
        assert await ledger_db.unchanged() == committed
    else:
        assert await ledger_db.unchanged() == before
        with pytest.raises(LoadError, match="LOAD_STAGING_NOT_SEALED"):
            await case.loader.execute(case.request)
        assert await ledger_db.unchanged() == before


@pytest.mark.parametrize("outcome", ["rollback", "committed"])
async def test_cleanup_failure_never_claims_false_rollback_or_loses_known_commit(
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    outcome: Literal["rollback", "committed"],
) -> None:
    case = await parent_case(ledger_db, pg_dsn)
    writer_connection: AsyncConnection | None = None
    pid = 0
    claim, close = Ledger.claim, AsyncConnection.close

    async def capture(
        self: Ledger, connection: AsyncConnection
    ) -> PostgreSQLLoadResult | None:
        nonlocal writer_connection, pid
        writer_connection = connection
        pid = (await connection.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one()
        return await claim(self, connection)

    async def broken_close(self: AsyncConnection) -> None:
        if self is writer_connection:
            raise OSError("cleanup-secret-canary")
        await close(self)

    async def fail() -> None:
        raise RuntimeError("late-write-secret-canary")

    monkeypatch.setattr(Ledger, "claim", capture)
    monkeypatch.setattr(AsyncConnection, "close", broken_close)
    if outcome == "rollback":
        monkeypatch.setattr(
            _load_transaction, "_write", after(_load_transaction._write, fail)
        )
    before = await ledger_db.db.snapshot()
    if outcome == "rollback":
        with pytest.raises(LoadError, match="LOAD_OUTCOME_UNKNOWN") as caught:
            await case.loader.execute(case.request)
        assert "secret-canary" not in str(caught.value)
        assert await ledger_db.db.snapshot() == before
        await assert_empty_ledger(ledger_db)
        expected = StagingRunStatus.UNKNOWN
    else:
        result = await case.loader.execute(case.request)
        assert result.inserted == 1
        assert result.warnings == ("LOAD_POST_COMMIT_CLEANUP_FAILED",)
        assert await contents(ledger_db.db, "parents") == ((1, 10), (2, 20))
        assert len(await ledger_db.rows("execution_commits")) == 1
        expected = StagingRunStatus.COMMITTED
    assert (await case.store.get_run(case.request.staging_context)).status is expected
    await assert_released(ledger_db, pid)
