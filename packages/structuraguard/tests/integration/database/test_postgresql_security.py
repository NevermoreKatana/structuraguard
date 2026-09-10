"""Read-only, scope, credentials и cleanup на реальном PostgreSQL."""

from __future__ import annotations

import asyncio
import logging
import traceback

import pytest
from pydantic import SecretStr
from sqlalchemy import event, text
from sqlalchemy.engine import Connection, ExecutionContext, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from structuraguard.contracts import DatabaseInspectionRequest, DatabaseMetadataSnapshot
from structuraguard.database import (
    InspectionLimits,
    PostgreSQLDatabaseAdapter,
    PostgreSQLTarget,
)
from structuraguard.database import postgresql as adapter_module
from structuraguard.database._postgresql_catalog import PostgreSQLReader, reflect
from structuraguard.exceptions import DatabaseInspectionError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


def request(target: PostgreSQLTarget) -> DatabaseInspectionRequest:
    return DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )


async def no_connections(dsn: str) -> None:
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE application_name = 'structuraguard-inspection'"
                )
            )
            assert count == 0
    finally:
        await engine.dispose()


async def test_read_only_is_enforced_even_with_owner_credentials(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pg_target.model_copy(update={"dsn": SecretStr(pg_dsn)})
    original = reflect

    async def probe(
        reader: PostgreSQLReader, names: tuple[tuple[str, str], ...]
    ) -> DatabaseMetadataSnapshot:
        assert (
            await reader.connection.scalar(text("SHOW transaction_read_only")) == "on"
        )
        assert (
            await reader.connection.scalar(text("SHOW transaction_isolation"))
            == "serializable"
        )
        assert await reader.connection.scalar(text("SHOW statement_timeout")) == "10s"
        for statement in (
            "CREATE TABLE app.should_never_exist (id integer)",
            "UPDATE app.items SET qty = 77",
            "INSERT INTO app.items (qty) VALUES (77)",
            "DELETE FROM app.items",
            "TRUNCATE app.items",
        ):
            savepoint = await reader.connection.begin_nested()
            try:
                with pytest.raises(DBAPIError) as caught:
                    await reader.connection.exec_driver_sql(statement)
                assert getattr(caught.value.orig, "sqlstate", None) == "25006"
            finally:
                await savepoint.rollback()
        return await original(reader, names)

    monkeypatch.setattr(adapter_module, "reflect", probe)
    await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT qty FROM app.items")) == 4
            assert (
                await connection.scalar(
                    text("SELECT to_regclass('app.should_never_exist')")
                )
                is None
            )
    finally:
        await engine.dispose()
    await no_connections(pg_dsn)


async def test_scope_precedes_reflection_and_role_cannot_read_rows(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace: list[tuple[str, object]] = []
    original = PostgreSQLDatabaseAdapter._engine

    def engine_factory(adapter: PostgreSQLDatabaseAdapter) -> AsyncEngine:
        engine = original(adapter)

        def record(
            connection: Connection,
            cursor: object,
            statement: str,
            parameters: object,
            context: ExecutionContext,
            executemany: bool,
        ) -> None:
            trace.append((statement, parameters))

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        return engine

    monkeypatch.setattr(PostgreSQLDatabaseAdapter, "_engine", engine_factory)
    target = pg_target.model_copy(
        update={
            "include_schemas": (*pg_target.include_schemas, "forbidden"),
            "deny_schemas": ("forbidden",),
            "include_tables": (
                *pg_target.include_tables,
                ("forbidden", "items"),
                ("app", "hidden"),
            ),
            "deny_tables": (("app", "hidden"),),
        }
    )
    snapshot = await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert all(t.name != "hidden" for s in snapshot.schemas for t in s.tables)
    assert all(s.name != "forbidden" for s in snapshot.schemas)
    assert trace and all(sql.lstrip().upper().startswith("SELECT") for sql, _ in trace)
    assert all(
        "forbidden" not in repr(params) and "hidden" not in repr(params)
        for _, params in trace
    )
    assert not any("FROM app." in sql or "FROM ref." in sql for sql, _ in trace)
    engine = create_async_engine(pg_target.dsn.get_secret_value(), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError) as caught:
                await connection.exec_driver_sql("SELECT * FROM app.items")
            assert getattr(caught.value.orig, "sqlstate", None) == "42501"
    finally:
        await engine.dispose()
    await no_connections(pg_dsn)


@pytest.mark.parametrize("bad_password", [False, True])
async def test_permission_error_is_typed_and_credentials_are_redacted(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    bad_password: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dsn = (
        make_url(pg_dsn)
        .set(
            username="inspector" if bad_password else "blocked",
            password="wrong-canary-938" if bad_password else "inspection-canary-47",
        )
        .render_as_string(hide_password=False)
    )
    target = pg_target.model_copy(update={"dsn": SecretStr(dsn)})
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.pool")
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_PERMISSION_DENIED"
    output = (
        str(caught.value)
        + repr(caught.value)
        + "".join(traceback.format_exception(caught.value))
        + caplog.text
        + repr(target)
        + target.model_dump_json()
    )
    assert "wrong-canary-938" not in output
    assert "inspection-canary-47" not in output
    assert dsn not in output
    assert caught.value.__context__ is None
    await no_connections(pg_dsn)


async def test_debug_logging_does_not_publish_metadata(
    pg_target: PostgreSQLTarget,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    await PostgreSQLDatabaseAdapter(pg_target).inspect_metadata(request(pg_target))
    assert "Служебное описание" not in caplog.text
    assert "Количество" not in caplog.text
    assert "inspection-canary-47" not in caplog.text


async def test_foreign_key_does_not_expand_scope(
    pg_target: PostgreSQLTarget, pg_dsn: str
) -> None:
    target = pg_target.model_copy(update={"deny_tables": (("ref", "parents"),)})
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_CATALOG_SCOPE_INCOMPLETE"
    await no_connections(pg_dsn)


async def test_request_schema_restriction_also_bounds_type_reflection(
    pg_target: PostgreSQLTarget,
) -> None:
    target = pg_target.model_copy(update={"include_tables": (("app", "remote_type"),)})
    adapter = PostgreSQLDatabaseAdapter(target)
    await adapter.inspect_metadata(request(target))
    narrowed = request(target).model_copy(update={"schema_names": ("app",)})
    with pytest.raises(DatabaseInspectionError) as caught:
        await adapter.inspect_metadata(narrowed)
    assert caught.value.error_code == "DATABASE_CATALOG_SCOPE_INCOMPLETE"


@pytest.mark.parametrize(
    "limits",
    [
        InspectionLimits(max_columns=1),
        InspectionLimits(max_constraints=1),
        InspectionLimits(max_items=2),
        InspectionLimits(max_metadata_bytes=32),
        InspectionLimits(max_text_chars=3),
    ],
)
async def test_limits_fail_without_partial_catalog(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    limits: InspectionLimits,
) -> None:
    target = pg_target.model_copy(update={"limits": limits})
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    await no_connections(pg_dsn)


async def test_server_statement_timeout_closes_connection(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pg_target.model_copy(
        update={"limits": InspectionLimits(statement_timeout_seconds=0.03)}
    )

    async def slow(
        reader: PostgreSQLReader, names: tuple[tuple[str, str], ...]
    ) -> DatabaseMetadataSnapshot:
        await reader.connection.exec_driver_sql("SELECT pg_catalog.pg_sleep(5)")
        pytest.fail("statement_timeout не сработал")

    monkeypatch.setattr(adapter_module, "reflect", slow)
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "PROCESSING_TIMEOUT"
    await no_connections(pg_dsn)


async def test_cancellation_stops_active_query_and_releases_connection(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()
    real_close = AsyncConnection.close

    async def controlled_close(connection: AsyncConnection) -> None:
        if connection.engine.url.username == "inspector":
            closing.set()
            await release_close.wait()
        await real_close(connection)

    async def slow(
        reader: PostgreSQLReader, names: tuple[tuple[str, str], ...]
    ) -> DatabaseMetadataSnapshot:
        entered.set()
        await reader.connection.exec_driver_sql("SELECT pg_catalog.pg_sleep(30)")
        pytest.fail("отменённый query завершился без cancellation")

    monkeypatch.setattr(adapter_module, "reflect", slow)
    monkeypatch.setattr(AsyncConnection, "close", controlled_close)
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    task = asyncio.create_task(adapter.inspect_metadata(request(pg_target)))
    async with asyncio.timeout(5):
        await entered.wait()
        observer = create_async_engine(pg_dsn, poolclass=NullPool)
        try:
            async with observer.connect() as connection:
                while not await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE "
                        "application_name = 'structuraguard-inspection' AND state = 'active' "
                        "AND query LIKE '%pg_sleep%'"
                    )
                ):
                    await connection.rollback()
        finally:
            await observer.dispose()
        with pytest.raises(DatabaseInspectionError) as busy:
            await adapter.inspect_metadata(request(pg_target))
        assert busy.value.error_code == "DATABASE_INSPECTION_BUSY"
        task.cancel()
        await closing.wait()
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    await no_connections(pg_dsn)


async def test_total_timeout_closes_connection(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pg_target.model_copy(
        update={"limits": InspectionLimits(timeout_seconds=0.3)}
    )

    async def slow(
        reader: PostgreSQLReader, names: tuple[tuple[str, str], ...]
    ) -> DatabaseMetadataSnapshot:
        await reader.connection.exec_driver_sql("SELECT pg_catalog.pg_sleep(5)")
        pytest.fail("общий deadline не сработал")

    monkeypatch.setattr(adapter_module, "reflect", slow)
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "PROCESSING_TIMEOUT"
    await no_connections(pg_dsn)


async def test_lock_timeout_during_reflection_closes_connection(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
) -> None:
    target = pg_target.model_copy(
        update={"limits": InspectionLimits(lock_timeout_seconds=0.03)}
    )
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.connect() as blocker:
            await blocker.exec_driver_sql(
                "LOCK TABLE app.items IN ACCESS EXCLUSIVE MODE"
            )
            with pytest.raises(DatabaseInspectionError) as caught:
                await PostgreSQLDatabaseAdapter(target).inspect_metadata(
                    request(target)
                )
            assert caught.value.error_code == "PROCESSING_TIMEOUT"
    finally:
        await engine.dispose()
    await no_connections(pg_dsn)


async def test_cleanup_deadline_terminates_driver_and_blocks_reuse(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = pg_target.model_copy(
        update={"limits": InspectionLimits(cleanup_seconds=0.03)}
    )
    real_close = AsyncConnection.close
    blocked = asyncio.Event()

    async def stuck_close(connection: AsyncConnection) -> None:
        if connection.engine.url.username == "inspector":
            await blocked.wait()
        await real_close(connection)

    monkeypatch.setattr(AsyncConnection, "close", stuck_close)
    adapter = PostgreSQLDatabaseAdapter(target)
    with pytest.raises(DatabaseInspectionError) as caught:
        await adapter.inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_INSPECTION_CLEANUP_FAILED"
    with pytest.raises(DatabaseInspectionError) as caught:
        await adapter.inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_INSPECTION_BUSY"
    await no_connections(pg_dsn)
