"""Настоящие PostgreSQL 16/18 prechecks; inspector отличается от writer."""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes.mapping import refs
from tests.unit.validation.test_db_constraints import data, integer, policy

from structuraguard.contracts.constraint_validation import (
    ConstraintReadPolicy,
    ConstraintTablePolicy,
)
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.database.constraint_reader import DatabaseConstraintReader
from structuraguard.validation import DatabaseConstraintValidator

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("precision", [0, 3])
async def test_pg_timestamp_precision_cannot_hide_unique_conflicts(
    pg_dsn: str,
    pg_target: PostgreSQLTarget,
    precision: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from typing import Never

    from structuraguard.contracts.common import DateTimeScalar
    from structuraguard.contracts.constraint_validation import (
        ConstraintLookup,
        ConstraintReadRequest,
    )
    from structuraguard.exceptions import DatabaseInspectionError

    namespace = f"m12_temporal_{precision}"
    value = datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=UTC)
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {namespace}"))
            await connection.execute(
                text(
                    f"CREATE TABLE {namespace}.events(at timestamp({precision}) with time zone UNIQUE)"
                )
            )
            await connection.execute(
                text(f"INSERT INTO {namespace}.events VALUES (:value)"),
                {"value": value},
            )
            stored = (
                await connection.execute(text(f"SELECT at FROM {namespace}.events"))
            ).scalar_one()
            assert stored != value
            await connection.execute(
                text(f"GRANT USAGE ON SCHEMA {namespace} TO inspector")
            )
            await connection.execute(
                text(f"GRANT SELECT ON ALL TABLES IN SCHEMA {namespace} TO inspector")
            )
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="temporal-review",
            include_schemas=(namespace,),
            include_tables=((namespace, "events"),),
        )
        catalog = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        table = catalog.schemas[0].tables[0]
        column = table.columns[0]
        dataset = data(
            (table.table_id, {column.column_id: {"kind": "datetime", "value": value}})
        )
        before = dataset.canonical_json()

        def forbidden(*args: object, **kwargs: object) -> Never:
            raise AssertionError("Unverified temporal precision reached DB I/O")

        monkeypatch.setattr(PostgreSQLDatabaseAdapter, "_engine", forbidden)
        reader = DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(catalog))
        )
        report = await DatabaseConstraintValidator(
            policy(catalog), reader=reader
        ).validate(dataset, catalog=catalog)
        assert not report.accepted
        assert [issue.code for issue in report.issues] == ["DB_CONSTRAINT_UNVERIFIED"]
        assert report.read_snapshot_fingerprint is None
        assert dataset.canonical_json() == before

        request = ConstraintReadRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
            database_fingerprint=catalog.database_fingerprint,
            lookups=(
                ConstraintLookup(
                    lookup_id="temporal",
                    table_id=table.table_id,
                    column_ids=(column.column_id,),
                    values=(DateTimeScalar(value=value),),
                ),
            ),
        )
        with pytest.raises(DatabaseInspectionError) as error:
            await reader.read(request, catalog=catalog)
        assert error.value.error_code == "DB_CONSTRAINT_UNVERIFIED"
    finally:
        await engine.dispose()


async def test_pg_composite_upsert_and_fk(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA m12_constraints"))
            await connection.execute(
                text(
                    "CREATE TABLE m12_constraints.parents(a integer NOT NULL, b integer NOT NULL, c integer UNIQUE, PRIMARY KEY(a,b))"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE m12_constraints.children(x integer, y integer, FOREIGN KEY(x,y) REFERENCES m12_constraints.parents(a,b))"
                )
            )
            await connection.execute(
                text("INSERT INTO m12_constraints.parents VALUES(1,2,3),(4,5,6)")
            )
            await connection.execute(
                text("GRANT USAGE ON SCHEMA m12_constraints TO inspector")
            )
            await connection.execute(
                text(
                    "GRANT SELECT ON ALL TABLES IN SCHEMA m12_constraints TO inspector"
                )
            )
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="m12-keys",
            include_schemas=("m12_constraints",),
            include_tables=(
                ("m12_constraints", "parents"),
                ("m12_constraints", "children"),
            ),
        )
        db = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        tables = {t.name: t for s in db.schemas for t in s.tables}
        parent, child = tables["parents"], tables["children"]
        pc = {c.name: c.column_id for c in parent.columns}
        cc = {c.name: c.column_id for c in child.columns}
        reader = DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(db), chunk_size=1)
        )
        configured = policy(db).model_copy(
            update={
                "tables": (
                    ConstraintTablePolicy(
                        table_id=parent.table_id,
                        operation="upsert",
                        identity_column_ids=(pc["a"], pc["b"]),
                    ),
                    ConstraintTablePolicy(table_id=child.table_id),
                )
            }
        )
        validator = DatabaseConstraintValidator(configured, reader=reader)
        accepted = await validator.validate(
            data(
                (
                    parent.table_id,
                    {pc["a"]: integer(1), pc["b"]: integer(2), pc["c"]: integer(3)},
                ),
                (child.table_id, {cc["x"]: integer(4), cc["y"]: integer(5)}),
            ),
            catalog=db,
        )
        assert accepted.accepted, [(i.code, i.field_ids) for i in accepted.issues]
        conflict = await validator.validate(
            data(
                (
                    parent.table_id,
                    {pc["a"]: integer(1), pc["b"]: integer(2), pc["c"]: integer(6)},
                ),
                (child.table_id, {cc["x"]: integer(0), cc["y"]: integer(0)}),
            ),
            catalog=db,
        )
        assert {i.code for i in conflict.issues} == {"DB_UNIQUE", "DB_FOREIGN_KEY"}
        async with engine.connect() as connection:
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM m12_constraints.parents")
                )
            ).scalar_one() == 2
            assert (
                await connection.execute(
                    text("SELECT count(*) FROM m12_constraints.children")
                )
            ).scalar_one() == 0
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DROP SCHEMA IF EXISTS m12_constraints CASCADE")
            )
        await engine.dispose()


async def test_pg_nulls_not_distinct_and_rls_are_explicit(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    from structuraguard.exceptions import DatabaseInspectionError

    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA m12_nulls"))
            await connection.execute(
                text(
                    "CREATE TABLE m12_nulls.items(a integer, b integer, UNIQUE NULLS NOT DISTINCT(a,b))"
                )
            )
            await connection.execute(
                text("INSERT INTO m12_nulls.items VALUES(NULL,NULL)")
            )
            await connection.execute(
                text("GRANT USAGE ON SCHEMA m12_nulls TO inspector")
            )
            await connection.execute(
                text("GRANT SELECT ON m12_nulls.items TO inspector")
            )
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="m12-nulls",
            include_schemas=("m12_nulls",),
            include_tables=(("m12_nulls", "items"),),
        )
        db = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        table = db.schemas[0].tables[0]
        source = data(
            (table.table_id, {c.column_id: {"kind": "null"} for c in table.columns})
        )
        validator = DatabaseConstraintValidator(
            policy(db),
            reader=DatabaseConstraintReader(
                target, policy=ConstraintReadPolicy(allow_columns=refs(db))
            ),
        )
        report = await validator.validate(source, catalog=db)
        assert [i.code for i in report.issues] == ["DB_UNIQUE"]
        async with engine.begin() as connection:
            await connection.execute(
                text("ALTER TABLE m12_nulls.items ENABLE ROW LEVEL SECURITY")
            )
        with pytest.raises(DatabaseInspectionError) as error:
            await validator.validate(source, catalog=db)
        assert error.value.error_code == "DB_CONSTRAINT_UNVERIFIED"
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS m12_nulls CASCADE"))
        await engine.dispose()


async def test_pg_schema_is_locked_before_catalog_snapshot(
    pg_dsn: str, pg_target: PostgreSQLTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.contracts.database import DatabaseMetadataSnapshot
    from structuraguard.database import constraint_reader as module
    from structuraguard.database._postgresql_catalog import PostgreSQLReader, reflect

    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    original = reflect
    inspected = False

    async def locked_reflect(
        reader: PostgreSQLReader, names: tuple[tuple[str, str], ...]
    ) -> DatabaseMetadataSnapshot:
        nonlocal inspected
        count = (
            await reader.connection.execute(
                text(
                    "SELECT count(*) FROM pg_catalog.pg_locks WHERE pid=pg_backend_pid() AND relation='m12_locks.items'::regclass AND mode='AccessShareLock' AND granted"
                )
            )
        ).scalar_one()
        assert count >= 1
        inspected = True
        return await original(reader, names)

    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA m12_locks"))
            await connection.execute(
                text("CREATE TABLE m12_locks.items(id integer PRIMARY KEY)")
            )
            await connection.execute(
                text("GRANT USAGE ON SCHEMA m12_locks TO inspector")
            )
            await connection.execute(
                text("GRANT SELECT ON m12_locks.items TO inspector")
            )
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="m12-locks",
            include_schemas=("m12_locks",),
            include_tables=(("m12_locks", "items"),),
        )
        db = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        table = db.schemas[0].tables[0]
        monkeypatch.setattr(module, "reflect", locked_reflect)
        validator = DatabaseConstraintValidator(
            policy(db),
            reader=DatabaseConstraintReader(
                target, policy=ConstraintReadPolicy(allow_columns=refs(db))
            ),
        )
        assert (
            await validator.validate(
                data((table.table_id, {table.columns[0].column_id: integer(1)})),
                catalog=db,
            )
        ).accepted
        assert inspected
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS m12_locks CASCADE"))
        await engine.dispose()


async def test_pg_fk_can_read_generated_always_parent_identity(
    pg_dsn: str, pg_target: PostgreSQLTarget
) -> None:
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA m12_identity"))
            await connection.execute(
                text(
                    "CREATE TABLE m12_identity.parents(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE m12_identity.children(parent_id bigint REFERENCES m12_identity.parents(id))"
                )
            )
            await connection.execute(
                text("INSERT INTO m12_identity.parents DEFAULT VALUES")
            )
            await connection.execute(
                text("GRANT USAGE ON SCHEMA m12_identity TO inspector")
            )
            await connection.execute(
                text("GRANT SELECT ON ALL TABLES IN SCHEMA m12_identity TO inspector")
            )
        target = PostgreSQLTarget(
            dsn=pg_target.dsn,
            target_id="m12-identity",
            include_schemas=("m12_identity",),
            include_tables=(("m12_identity", "parents"), ("m12_identity", "children")),
        )
        db = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        child = next(t for s in db.schemas for t in s.tables if t.name == "children")
        validator = DatabaseConstraintValidator(
            policy(db),
            reader=DatabaseConstraintReader(
                target, policy=ConstraintReadPolicy(allow_columns=refs(db))
            ),
        )
        report = await validator.validate(
            data(
                (child.table_id, {child.columns[0].column_id: integer(1)}),
                (child.table_id, {child.columns[0].column_id: integer(2)}),
            ),
            catalog=db,
        )
        assert [(i.record_id, i.code) for i in report.issues] == [
            ("1", "DB_FOREIGN_KEY")
        ]
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA IF EXISTS m12_identity CASCADE"))
        await engine.dispose()
