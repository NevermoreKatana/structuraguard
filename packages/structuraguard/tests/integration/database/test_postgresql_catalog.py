"""Fingerprint-bound PostgreSQL catalog и drift на реальном сервере."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.contract_suites.database import (
    assert_execute_unavailable,
    assert_inspection_contract,
)

from structuraguard.contracts import (
    DatabaseInspectionRequest,
)
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.domain import (
    verify_database_fingerprint,
)
from structuraguard.exceptions import (
    DatabaseInspectionError,
)
from structuraguard.ports.database import DatabaseAdapter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


def request(target: PostgreSQLTarget) -> DatabaseInspectionRequest:
    return DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )


async def test_full_port_catalog_and_composite_fk(pg_target: PostgreSQLTarget) -> None:
    adapter: DatabaseAdapter = PostgreSQLDatabaseAdapter(pg_target)
    result = await assert_inspection_contract(adapter, request(pg_target))
    graph = result.dependency_graph
    assert graph is not None and graph.load_order is not None
    tables = {table.name: table for schema in result.schemas for table in schema.tables}
    assert graph.load_order == (tables["parents"].table_id, tables["items"].table_id)
    assert len(graph.table_ids) == 4
    (edge,) = graph.edges
    assert edge.parent_column_ids == tables["parents"].primary_key
    assert edge.child_column_ids == tables["items"].foreign_keys[0].column_ids
    assert len(edge.parent_column_ids) == 2


async def test_data_only_changes_preserve_hash_and_comment_change_is_drift(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    original = await adapter.inspect(request(pg_target))
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE app.items SET qty = 5")
        verify_database_fingerprint(
            await adapter.inspect(request(pg_target)), original.database_fingerprint
        )
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "COMMENT ON COLUMN app.items.qty IS 'Изменённое описание'"
            )
        with pytest.raises(DatabaseInspectionError) as caught:
            verify_database_fingerprint(
                await adapter.inspect(request(pg_target)), original.database_fingerprint
            )
        assert caught.value.error_code == "DATABASE_SCHEMA_DRIFT"
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("UPDATE app.items SET qty = 4")
            await connection.exec_driver_sql(
                "COMMENT ON COLUMN app.items.qty IS 'Количество'"
            )
        await engine.dispose()
    verify_database_fingerprint(
        await adapter.inspect(request(pg_target)), original.database_fingerprint
    )


async def test_execute_is_explicitly_unavailable(pg_target: PostgreSQLTarget) -> None:
    await assert_execute_unavailable(PostgreSQLDatabaseAdapter(pg_target))
