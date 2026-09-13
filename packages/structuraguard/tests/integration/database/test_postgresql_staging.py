"""PostgreSQL 16/18: общие contracts, DDL isolation, атомарность и гонки staging."""

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from pydantic import SecretStr
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.contract.stores._suite import StagingContract
from tests.contract.stores.test_memory_staging import case as case
from tests.contract.stores.test_memory_staging import clock as clock
from tests.contract.stores.test_memory_staging import policy as policy
from tests.fakes.staging import StagingCase, StagingClock

from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.staging import (
    StagingLimits,
    StagingRetentionPolicy,
    StagingRunStatus,
)
from structuraguard.exceptions import StagingError
from structuraguard.stores import PostgreSQLStagingStore, PostgreSQLStagingTarget
from structuraguard.stores._core import State
from structuraguard.stores.bootstrap import bootstrap_postgresql_staging

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.database_integration,
]


@dataclass(frozen=True)
class Targets:
    admin: PostgreSQLStagingTarget
    writer: PostgreSQLStagingTarget
    cleaner: PostgreSQLStagingTarget


@pytest.fixture
async def targets(
    pg_dsn: str, stage_accounts: None, request: pytest.FixtureRequest
) -> AsyncIterator[Targets]:
    suffix = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:16]
    schema = f"sg_staging_{suffix}"
    url = make_url(pg_dsn)

    def target(principal: str, purpose: str) -> PostgreSQLStagingTarget:
        dsn = (
            pg_dsn
            if purpose == "bootstrap"
            else url.set(
                username=principal, password="staging-test-47"
            ).render_as_string(hide_password=False)
        )
        return PostgreSQLStagingTarget.model_validate(
            {
                "dsn": SecretStr(dsn),
                "principal": principal,
                "inspector_principal": "inspector",
                "target_id": "main",
                "namespace": "app-1",
                "schema_name": schema,
                "purpose": purpose,
            }
        )

    assert url.username is not None
    result = Targets(
        target(url.username, "bootstrap"),
        target("staging_writer", "writer"),
        target("staging_cleaner", "maintenance"),
    )
    await bootstrap_postgresql_staging(result.admin)
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            # schema — только hex digest nodeid; административная fixture вне SDK.
            await connection.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO staging_writer, staging_cleaner'
            )
            await connection.exec_driver_sql(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO staging_writer, staging_cleaner'
            )
            await connection.exec_driver_sql(
                f'GRANT INSERT, UPDATE ON "{schema}".runs TO staging_writer'
            )
            await connection.exec_driver_sql(
                f'GRANT INSERT ON "{schema}".batches, "{schema}".records TO staging_writer'
            )
            await connection.exec_driver_sql(
                f'GRANT UPDATE ON "{schema}".runs TO staging_cleaner'
            )
            await connection.exec_driver_sql(
                f'GRANT DELETE ON "{schema}".batches, "{schema}".records TO staging_cleaner'
            )
        yield result
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        await engine.dispose()


@pytest.fixture
def store(
    targets: Targets, clock: StagingClock, policy: StagingRetentionPolicy
) -> PostgreSQLStagingStore:
    return PostgreSQLStagingStore(targets.writer, retention=policy, clock=clock)


@pytest.fixture
def cleaner(
    targets: Targets, clock: StagingClock, policy: StagingRetentionPolicy
) -> PostgreSQLStagingStore:
    return PostgreSQLStagingStore(targets.cleaner, retention=policy, clock=clock)


class TestPostgreSQLStaging(StagingContract):
    pass


async def test_bootstrap_is_explicit_repeatable_and_writer_has_no_ddl(
    targets: Targets, store: PostgreSQLStagingStore, case: StagingCase
) -> None:
    from structuraguard.stores._postgresql_schema import check_schema

    admin = create_async_engine(
        targets.admin.dsn.get_secret_value(), poolclass=NullPool
    )
    try:
        async with admin.begin() as connection:
            await check_schema(connection, targets.admin.schema_name)
    finally:
        await admin.dispose()
    await bootstrap_postgresql_staging(targets.admin)
    with pytest.raises(StagingError, match="STAGING_PRINCIPAL_FORBIDDEN"):
        await bootstrap_postgresql_staging(targets.writer)
    with pytest.raises(StagingError, match="STAGING_PRINCIPAL_FORBIDDEN"):
        await store.cleanup(case.spec.context)
    engine = create_async_engine(
        targets.writer.dsn.get_secret_value(), poolclass=NullPool
    )
    try:
        from sqlalchemy.exc import DBAPIError

        for statement in (
            "CREATE TABLE app.staging_escape(id integer)",
            "ALTER TABLE app.items ADD COLUMN staging_escape integer",
        ):
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def test_runtime_never_creates_missing_schema(
    targets: Targets,
    clock: StagingClock,
    policy: StagingRetentionPolicy,
    case: StagingCase,
) -> None:
    missing = targets.writer.model_copy(update={"schema_name": "sg_staging_missing"})
    store = PostgreSQLStagingStore(missing, retention=policy, clock=clock)
    with pytest.raises(StagingError, match="STAGING_SCHEMA_UNAVAILABLE"):
        await store.begin(case.spec)


async def test_batch_failure_and_cancellation_rollback_all_metadata(
    store: PostgreSQLStagingStore, case: StagingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await store.begin(case.spec)
    original = store._save
    written = asyncio.Event()
    hold = asyncio.Event()

    async def pause(
        connection: AsyncConnection,
        context: StagingContext,
        previous: State | None,
        state: State,
    ) -> None:
        await original(connection, context, previous, state)
        written.set()
        await hold.wait()

    monkeypatch.setattr(store, "_save", pause)
    task = asyncio.create_task(store.stage(case.batches[0], case.spec.context))
    await asyncio.wait_for(written.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(store, "_save", original)
    assert await store.get_run(case.spec.context) == before
    await store.stage(case.batches[0], case.spec.context)
    assert (await store.get_run(case.spec.context)).batch_count == 1


async def test_concurrent_batch_replay_and_cas(
    targets: Targets,
    store: PostgreSQLStagingStore,
    case: StagingCase,
    clock: StagingClock,
    policy: StagingRetentionPolicy,
) -> None:
    other = PostgreSQLStagingStore(targets.writer, retention=policy, clock=clock)
    ctx = case.spec.context
    first, second = await asyncio.gather(store.begin(case.spec), other.begin(case.spec))
    assert first == second
    for batch in case.batches:
        await asyncio.gather(store.stage(batch, ctx), other.stage(batch, ctx))
    before = await store.get_run(ctx)
    results = await asyncio.gather(
        store.seal(ctx, expected_revision=before.revision),
        other.seal(ctx, expected_revision=before.revision),
        return_exceptions=True,
    )
    assert sum(isinstance(r, StagingError) for r in results) == 1
    assert (await store.get_run(ctx)).status is StagingRunStatus.SEALED


async def test_staging_values_are_bound_and_driver_logs_do_not_leak(
    store: PostgreSQLStagingStore,
    case: StagingCase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from structuraguard.stores import postgresql

    original = postgresql._engine
    statements: list[str] = []

    def engine(target: PostgreSQLStagingTarget, limits: StagingLimits) -> AsyncEngine:
        result = original(target, limits)

        def capture(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(result.sync_engine, "before_cursor_execute", capture)
        return result

    monkeypatch.setattr(postgresql, "_engine", engine)
    canary = "x'; DROP SCHEMA app CASCADE; --"
    refs = tuple(
        r.model_copy(update={"artifact_id": canary}) for r in case.spec.references
    )
    spec = case.spec.model_copy(update={"references": refs})
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    await store.begin(spec)
    for batch in case.batches:
        await store.stage(batch, spec.context)
    assert all(canary not in statement for statement in statements)
    assert any(
        "$1" in statement and "INSERT INTO" in statement for statement in statements
    )
    assert canary not in caplog.text and "artifact-" not in caplog.text
    assert not any(
        statement.lstrip()
        .upper()
        .startswith(("CREATE", "ALTER", "DROP", "GRANT", "TRUNCATE"))
        for statement in statements
    )


async def test_schema_drift_fails_before_run_write(
    targets: Targets, store: PostgreSQLStagingStore, case: StagingCase
) -> None:
    engine = create_async_engine(
        targets.admin.dsn.get_secret_value(), poolclass=NullPool
    )
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                f'ALTER TABLE "{targets.admin.schema_name}".records DROP COLUMN record_id'
            )
        with pytest.raises(StagingError, match="STAGING_SCHEMA_UNAVAILABLE"):
            await store.begin(case.spec)
    finally:
        await engine.dispose()


async def test_namespace_isolation_and_durable_reopen(
    targets: Targets,
    store: PostgreSQLStagingStore,
    case: StagingCase,
    policy: StagingRetentionPolicy,
    clock: StagingClock,
) -> None:
    await store.begin(case.spec)
    await store.stage(case.batches[0], case.spec.context)
    reopened = PostgreSQLStagingStore(targets.writer, retention=policy, clock=clock)
    assert (await reopened.get_run(case.spec.context)).batch_count == 1
    other = PostgreSQLStagingStore(
        targets.writer.model_copy(update={"namespace": "app-2"}),
        retention=policy,
        clock=clock,
    )
    with pytest.raises(StagingError, match="STAGING_RUN_NOT_FOUND"):
        await other.get_run(case.spec.context)
    assert (await other.begin(case.spec)).batch_count == 0
    assert (await store.get_run(case.spec.context)).batch_count == 1


async def test_lost_commit_reply_requires_reconciliation_and_replay_is_safe(
    store: PostgreSQLStagingStore, case: StagingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store.begin(case.spec)
    original = AsyncConnection.commit

    async def lost_reply(connection: AsyncConnection) -> None:
        await original(connection)
        raise OSError("driver-secret-canary")

    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "commit", lost_reply)
        with pytest.raises(StagingError, match="STAGING_OUTCOME_UNKNOWN") as error:
            await store.stage(case.batches[0], case.spec.context)
        assert "driver-secret-canary" not in str(error.value)
    committed = await store.get_run(case.spec.context)
    assert committed.batch_count == 1
    await store.stage(case.batches[0], case.spec.context)
    assert await store.get_run(case.spec.context) == committed


async def test_late_sql_failure_rolls_back_batch_and_records(
    store: PostgreSQLStagingStore, case: StagingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = await store.begin(case.spec)
    original = store._save

    async def fail(
        connection: AsyncConnection,
        context: StagingContext,
        previous: State | None,
        state: State,
    ) -> None:
        from sqlalchemy import text

        await original(connection, context, previous, state)
        await connection.execute(text("SELECT 1 / 0"))

    with monkeypatch.context() as patch:
        patch.setattr(store, "_save", fail)
        with pytest.raises(StagingError, match="STAGING_STORAGE_FAILED"):
            await store.stage(case.batches[0], case.spec.context)
    assert await store.get_run(case.spec.context) == before


@pytest.mark.parametrize(
    "change",
    [
        "rls",
        "rule",
        "trigger",
        "privileges",
        "prefix_privileges",
        "expression_index",
        "foreign_key",
    ],
)
async def test_runtime_schema_and_principal_abuse_is_rejected(
    targets: Targets, store: PostgreSQLStagingStore, case: StagingCase, change: str
) -> None:
    engine = create_async_engine(
        targets.admin.dsn.get_secret_value(), poolclass=NullPool
    )
    schema = targets.admin.schema_name
    try:
        async with engine.begin() as connection:
            if change == "rls":
                await connection.exec_driver_sql(
                    f'ALTER TABLE "{schema}".runs ENABLE ROW LEVEL SECURITY'
                )
            elif change == "rule":
                await connection.exec_driver_sql(
                    f'CREATE RULE ignore_insert AS ON INSERT TO "{schema}".runs DO INSTEAD NOTHING'
                )
            elif change == "trigger":
                await connection.exec_driver_sql(
                    f'CREATE FUNCTION "{schema}".noop() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$'
                )
                await connection.exec_driver_sql(
                    f'CREATE TRIGGER noop BEFORE INSERT ON "{schema}".runs FOR EACH ROW EXECUTE FUNCTION "{schema}".noop()'
                )
            elif change == "expression_index":
                await connection.exec_driver_sql(
                    f'CREATE INDEX expression_index ON "{schema}".runs (lower(payload))'
                )
            elif change == "foreign_key":
                await connection.exec_driver_sql(
                    f'ALTER TABLE "{schema}".records DROP CONSTRAINT records_namespace_target_id_run_id_fkey'
                )
                await connection.exec_driver_sql(
                    f'ALTER TABLE "{schema}".records ADD FOREIGN KEY (namespace,target_id,run_id) REFERENCES "{schema}".runs(target_id,namespace,run_id)'
                )
            elif change == "prefix_privileges":
                await connection.exec_driver_sql(
                    "CREATE SCHEMA pgx_staging_permission_test"
                )
                await connection.exec_driver_sql(
                    "GRANT CREATE ON SCHEMA pgx_staging_permission_test TO staging_writer"
                )
            else:
                await connection.exec_driver_sql(
                    "GRANT CREATE ON SCHEMA app TO staging_writer"
                )
        code = (
            "STAGING_PRINCIPAL_FORBIDDEN"
            if change in ("privileges", "prefix_privileges")
            else "STAGING_SCHEMA_UNAVAILABLE"
        )
        with pytest.raises(StagingError, match=code):
            await store.begin(case.spec)
    finally:
        if change == "privileges":
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "REVOKE CREATE ON SCHEMA app FROM staging_writer"
                )
        if change == "prefix_privileges":
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "DROP SCHEMA pgx_staging_permission_test CASCADE"
                )
        await engine.dispose()
