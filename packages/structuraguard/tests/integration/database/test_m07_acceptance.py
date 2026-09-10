"""K3/K4/K6: реальные missing/unsupported, credentials и concurrent DDL."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.integration.database.test_postgresql_catalog import request
from tests.integration.database.test_postgresql_security import no_connections

from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.database import _postgresql_queries as queries
from structuraguard.database._postgresql_catalog import PostgreSQLReader, Row
from structuraguard.exceptions import DatabaseInspectionError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


async def test_schema_and_constraint_comments_are_published(
    pg_target: PostgreSQLTarget,
) -> None:
    catalog = await PostgreSQLDatabaseAdapter(pg_target).inspect(request(pg_target))
    schema = next(schema for schema in catalog.schemas if schema.name == "app")
    assert schema.comment == "Прикладная схема"
    table = next(table for table in schema.tables if table.name == "items")
    assert table.inspection is not None
    key = next(
        item for item in table.inspection.constraints if item.name == "parent_fk"
    )
    assert key.comment == "Связь" and key.kind == "foreign_key"
    assert key.column_ids == table.foreign_keys[0].column_ids
    assert key.deferrable and key.initially_deferred and key.validated and key.enforced


async def test_connection_identity_does_not_change_schema_fingerprint(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    inspected = await PostgreSQLDatabaseAdapter(pg_target).inspect(request(pg_target))
    owner_target = pg_target.model_copy(update={"dsn": SecretStr(pg_dsn)})
    owned = await PostgreSQLDatabaseAdapter(owner_target).inspect(request(owner_target))
    assert inspected.database_fingerprint == owned.database_fingerprint
    assert inspected.dependency_graph == owned.dependency_graph
    await no_connections(pg_dsn)


async def test_unordered_reflection_rows_preserve_catalog_ids_and_hash(
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    before = await adapter.inspect(request(pg_target))
    original = PostgreSQLReader.rows
    reordered: set[str] = set()

    async def reverse_collections(
        reader: PostgreSQLReader, query: str, parameters: dict[str, object], limit: int
    ) -> list[Row]:
        rows = await original(reader, query, parameters, limit)
        if query in {
            queries.COLUMNS,
            queries.CONSTRAINTS,
            queries.CONSTRAINTS_18,
            queries.INDEXES,
            queries.DOMAIN_CHECKS,
        }:
            if len(rows) > 1:
                reordered.add(query)
            return list(reversed(rows))
        return rows

    monkeypatch.setattr(PostgreSQLReader, "rows", reverse_collections)
    after = await adapter.inspect(request(pg_target))
    assert queries.COLUMNS in reordered and queries.INDEXES in reordered
    assert after.database_fingerprint == before.database_fingerprint
    assert after == before


@pytest.mark.parametrize("missing", ["schema", "table"])
async def test_missing_object_is_typed_not_an_empty_catalog(
    pg_dsn: str, pg_target: PostgreSQLTarget, missing: str
) -> None:
    schema = "absent_schema" if missing == "schema" else "app"
    target = pg_target.model_copy(
        update={
            "include_schemas": (schema,),
            "include_tables": ((schema, "absent_table"),),
        }
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await PostgreSQLDatabaseAdapter(target).inspect(request(target))
    assert caught.value.error_code == "DATABASE_OBJECT_NOT_FOUND"
    await no_connections(pg_dsn)


async def test_exclusion_constraint_is_explicitly_unsupported(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_excluded(span int4range, EXCLUDE USING gist (span WITH &&))"
            )
        target = pg_target.model_copy(
            update={"include_tables": (("app", "m07_excluded"),)}
        )
        with pytest.raises(DatabaseInspectionError) as caught:
            await PostgreSQLDatabaseAdapter(target).inspect(request(target))
        assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"
        await no_connections(pg_dsn)
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE IF EXISTS app.m07_excluded")
        await engine.dispose()


async def test_concurrent_ddl_never_publishes_a_mixed_snapshot(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    target = pg_target.model_copy(update={"include_tables": (("app", "m07_snapshot"),)})
    adapter = PostgreSQLDatabaseAdapter(target)
    started, release = asyncio.Event(), asyncio.Event()
    original = PostgreSQLReader.rows
    pending: asyncio.Task[object] | None = None
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_snapshot(id integer PRIMARY KEY, marker integer DEFAULT 1)"
            )
        before = await adapter.inspect(request(target))

        async def pause_after_schema(
            reader: PostgreSQLReader,
            query: str,
            parameters: dict[str, object],
            limit: int,
        ) -> list[Row]:
            rows = await original(reader, query, parameters, limit)
            if query == queries.SCHEMA_COMMENT and not started.is_set():
                started.set()
                await release.wait()
            return rows

        monkeypatch.setattr(PostgreSQLReader, "rows", pause_after_schema)
        task = asyncio.create_task(adapter.inspect(request(target)))
        pending = task
        async with asyncio.timeout(10):
            await started.wait()
            async with engine.begin() as connection:
                await connection.exec_driver_sql("SET LOCAL statement_timeout = '3s'")
                await connection.exec_driver_sql(
                    "ALTER TABLE app.m07_snapshot ALTER COLUMN marker SET DEFAULT 2"
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE app.m07_snapshot ADD COLUMN added integer"
                )
                await connection.exec_driver_sql(
                    "COMMENT ON TABLE app.m07_snapshot IS 'after DDL'"
                )
            release.set()
            try:
                during = await task
            except DatabaseInspectionError as error:
                assert error.error_code in {
                    "DATABASE_INSPECTION_FAILED",
                    "DATABASE_METADATA_UNSUPPORTED",
                }
                during = None
        after = await adapter.inspect(request(target))
        assert after.database_fingerprint != before.database_fingerprint
        if during is not None:
            assert during.database_fingerprint in {
                before.database_fingerprint,
                after.database_fingerprint,
            }
        await no_connections(pg_dsn)
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE IF EXISTS app.m07_snapshot")
        await engine.dispose()
