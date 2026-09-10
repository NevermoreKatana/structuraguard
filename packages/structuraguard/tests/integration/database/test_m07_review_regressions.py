"""Regression финального review: domain CHECK drift и логический порядок колонок."""

from __future__ import annotations

import logging
import traceback

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.integration.database.test_postgresql_catalog import request
from tests.integration.database.test_postgresql_security import no_connections

from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.domain import verify_database_fingerprint
from structuraguard.exceptions import DatabaseInspectionError

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("change", ["validation", "comment", "name"])
async def test_domain_check_metadata_changes_fingerprint(
    pg_dsn: str, pg_target: PostgreSQLTarget, change: str
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    target = pg_target.model_copy(
        update={"include_tables": (("app", "m07_review_domain_table"),)}
    )
    adapter = PostgreSQLDatabaseAdapter(target)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE DOMAIN app.m07_review_domain AS integer"
            )
            await connection.exec_driver_sql(
                "ALTER DOMAIN app.m07_review_domain ADD CONSTRAINT positive_check "
                "CHECK (VALUE > 0) NOT VALID"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_review_domain_table(value app.m07_review_domain)"
            )
        before = await adapter.inspect(request(target))
        statements = {
            "validation": "ALTER DOMAIN app.m07_review_domain VALIDATE CONSTRAINT positive_check",
            "comment": "COMMENT ON CONSTRAINT positive_check ON DOMAIN app.m07_review_domain IS E' Смысл\\nограничения '",
            "name": "ALTER DOMAIN app.m07_review_domain RENAME CONSTRAINT positive_check TO renamed_check",
        }
        async with engine.begin() as connection:
            await connection.exec_driver_sql(statements[change])
        after = await adapter.inspect(request(target))
        assert after.database_fingerprint != before.database_fingerprint
        with pytest.raises(DatabaseInspectionError) as caught:
            verify_database_fingerprint(after, before.database_fingerprint)
        assert caught.value.error_code == "DATABASE_SCHEMA_DRIFT"
        before_column = before.schemas[0].tables[0].columns[0].inspection
        after_column = after.schemas[0].tables[0].columns[0].inspection
        assert before_column is not None and after_column is not None
        assert (
            before_column.data_type.domain_checks
            == after_column.data_type.domain_checks
        )
        checks = after_column.data_type.domain_constraints
        assert checks is not None and len(checks) == 1
        check = checks[0]
        assert check.name == ("renamed_check" if change == "name" else "positive_check")
        assert check.validated == (change == "validation")
        assert check.comment == (
            " Смысл\nограничения " if change == "comment" else None
        )
        await no_connections(pg_dsn)
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "DROP TABLE IF EXISTS app.m07_review_domain_table"
            )
            await connection.exec_driver_sql(
                "DROP DOMAIN IF EXISTS app.m07_review_domain"
            )
        await engine.dispose()


@pytest.mark.parametrize(
    ("change", "error_code"),
    [
        ("oversized_comment", "SECURITY_LIMIT_EXCEEDED"),
        ("secret_comment", "DATABASE_METADATA_UNSUPPORTED"),
        ("spaced_name", "DATABASE_METADATA_UNSUPPORTED"),
    ],
)
async def test_domain_check_metadata_remains_bounded_and_redacted(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    caplog: pytest.LogCaptureFixture,
    change: str,
    error_code: str,
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    target = pg_target.model_copy(
        update={"include_tables": (("app", "m07_review_domain_table"),)}
    )
    marker = "domain-review-secret-canary"
    statements = {
        "oversized_comment": "COMMENT ON CONSTRAINT positive_check ON DOMAIN app.m07_review_domain IS '"
        + "x" * 4097
        + "'",
        "secret_comment": "COMMENT ON CONSTRAINT positive_check ON DOMAIN app.m07_review_domain IS 'password=domain-review-secret-canary'",
        "spaced_name": 'ALTER DOMAIN app.m07_review_domain RENAME CONSTRAINT positive_check TO " padded "',
    }
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE DOMAIN app.m07_review_domain AS integer "
                "CONSTRAINT positive_check CHECK (VALUE > 0)"
            )
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_review_domain_table(value app.m07_review_domain)"
            )
            await connection.exec_driver_sql(statements[change])
        caplog.set_level(logging.DEBUG, logger="sqlalchemy")
        with pytest.raises(DatabaseInspectionError) as caught:
            await PostgreSQLDatabaseAdapter(target).inspect(request(target))
        caplog.set_level(logging.WARNING, logger="sqlalchemy")
        assert caught.value.error_code == error_code
        assert caught.value.__context__ is None and caught.value.__cause__ is None
        exposed = "".join(traceback.format_exception(caught.value)) + caplog.text
        assert marker not in exposed and "x" * 100 not in exposed
        await no_connections(pg_dsn)
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "DROP TABLE IF EXISTS app.m07_review_domain_table"
            )
            await connection.exec_driver_sql(
                "DROP DOMAIN IF EXISTS app.m07_review_domain"
            )
        await engine.dispose()


async def test_dropped_column_history_preserves_fingerprint_and_composite_keys(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    target = pg_target.model_copy(
        update={"include_tables": (("app", "m07_review_ordinal"),)}
    )
    adapter = PostgreSQLDatabaseAdapter(target)
    keys = (
        ", CONSTRAINT ordinal_pk PRIMARY KEY(b,a), "
        "CONSTRAINT ordinal_fk FOREIGN KEY(a,b) REFERENCES app.m07_review_ordinal(b,a))"
    )
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_review_ordinal(a integer, removed integer, b integer"
                + keys
            )
            await connection.exec_driver_sql(
                "ALTER TABLE app.m07_review_ordinal DROP COLUMN removed"
            )
        before = await adapter.inspect(request(target))
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE app.m07_review_ordinal")
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_review_ordinal(a integer, b integer" + keys
            )
        after = await adapter.inspect(request(target))
        assert after.database_fingerprint == before.database_fingerprint
        assert after == before
        table = before.schemas[0].tables[0]
        assert [
            c.inspection.ordinal_position for c in table.columns if c.inspection
        ] == [0, 1]
        names = {column.column_id: column.name for column in table.columns}
        assert tuple(names[column] for column in table.primary_key) == ("b", "a")
        fk = table.foreign_keys[0]
        assert tuple(names[column] for column in fk.column_ids) == ("a", "b")
        assert tuple(names[column] for column in fk.referenced_column_ids) == ("b", "a")
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP TABLE app.m07_review_ordinal")
            await connection.exec_driver_sql(
                "CREATE TABLE app.m07_review_ordinal(b integer, a integer" + keys
            )
        reordered = await adapter.inspect(request(target))
        assert reordered.database_fingerprint != before.database_fingerprint
        await no_connections(pg_dsn)
    finally:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(
                "DROP TABLE IF EXISTS app.m07_review_ordinal"
            )
        await engine.dispose()
