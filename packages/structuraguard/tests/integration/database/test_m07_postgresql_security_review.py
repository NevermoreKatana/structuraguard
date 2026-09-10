"""Security regression PostgreSQL: отказ при непредставимой column collation."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.integration.database.test_postgresql_catalog import request
from tests.integration.database.test_postgresql_security import no_connections

from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.exceptions import DatabaseInspectionError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("domain", [False, True])
async def test_unrepresented_column_collation_fails_closed(
    pg_dsn: str, pg_target: PostgreSQLTarget, domain: bool
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    target = pg_target.model_copy(update={"include_tables": (("app", "m07_collated"),)})
    adapter = PostgreSQLDatabaseAdapter(target)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_collated(value text)"
            )
        await adapter.inspect(request(target))
        async with engine.begin() as connection:
            if domain:
                await connection.exec_driver_sql(
                    'CREATE DOMAIN app.m07_collation_domain AS text COLLATE pg_catalog."C"'
                )
                await connection.exec_driver_sql(
                    "ALTER TABLE app.m07_collated ALTER COLUMN value TYPE app.m07_collation_domain"
                )
            else:
                await connection.exec_driver_sql(
                    'ALTER TABLE app.m07_collated ALTER COLUMN value TYPE text COLLATE pg_catalog."C"'
                )
        with pytest.raises(DatabaseInspectionError) as caught:
            await adapter.inspect(request(target))
        assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"
        await no_connections(pg_dsn)
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE IF EXISTS app.m07_collated")
            if domain:
                await connection.exec_driver_sql(
                    "DROP DOMAIN IF EXISTS app.m07_collation_domain"
                )
        await engine.dispose()
