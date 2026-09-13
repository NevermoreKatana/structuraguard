"""PostgreSQL 16/18: прогноз и нулевые изменения target/staging/sequences."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from hashlib import sha256

import pytest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.fakes.loading import dry_run_case
from tests.fakes.staging import StagingClock, staging_case

from structuraguard.contracts.common import IntegerScalar, LoadOperation
from structuraguard.contracts.database import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.contracts.loading import DryRunPolicy, DryRunRequest
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget
from structuraguard.database.dry_run import PostgreSQLDryRunPlanner
from structuraguard.exceptions import LoadError
from structuraguard.stores.bootstrap import bootstrap_postgresql_staging
from structuraguard.stores.postgresql import (
    PostgreSQLStagingStore,
    PostgreSQLStagingTarget,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@dataclass
class Database:
    engine: AsyncEngine
    target: PostgreSQLTarget
    catalog: DatabaseCatalog
    schema: str
    staging: str

    async def snapshot(self) -> tuple[object, ...]:
        async with self.engine.connect() as conn:
            rows: list[object] = []
            for schema, table in (
                (self.schema, "parents"),
                (self.schema, "children"),
                (self.schema, "keys"),
                *(
                    (self.staging, t)
                    for t in ("schema_info", "runs", "batches", "records")
                ),
            ):
                rows.append(
                    tuple(
                        (
                            await conn.exec_driver_sql(
                                f'SELECT xmin::text, ctid::text, t.* FROM "{schema}"."{table}" t ORDER BY 1,2'
                            )
                        ).all()
                    )
                )
            rows.append(
                tuple(
                    (
                        await conn.exec_driver_sql(
                            f'SELECT * FROM "{self.schema}".untouched'
                        )
                    ).all()
                )
            )
            return tuple(rows)

    async def sql(self, statement: str) -> None:
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql(statement.replace("$schema", f'"{self.schema}"'))


@pytest.fixture
async def dry_db(
    pg_dsn: str, request: pytest.FixtureRequest
) -> AsyncIterator[Database]:
    suffix = sha256(request.node.nodeid.encode()).hexdigest()[:16]
    schema = "dry_" + suffix
    staging = "sg_staging_dry_" + suffix
    engine = create_async_engine(pg_dsn, poolclass=NullPool)
    clock = StagingClock()
    retention = StagingRetentionPolicy()
    admin = PostgreSQLStagingTarget(
        dsn=SecretStr(pg_dsn),
        principal="test",
        inspector_principal="inspector",
        target_id="main",
        namespace="dry_run",
        schema_name=staging,
        purpose="bootstrap",
    )
    try:
        await bootstrap_postgresql_staging(admin)
        async with engine.begin() as conn:
            for sql in (
                f'CREATE SCHEMA "{schema}"',
                f'CREATE TABLE "{schema}".parents (id integer PRIMARY KEY, amount integer)',
                f'CREATE TABLE "{schema}".children (id integer PRIMARY KEY, parent integer REFERENCES "{schema}".parents(id), amount integer)',
                f'CREATE TABLE "{schema}".keys (id integer PRIMARY KEY)',
                f'CREATE SEQUENCE "{schema}".untouched',
                f'INSERT INTO "{schema}".parents VALUES (1, 10)',
                f'INSERT INTO "{schema}".keys VALUES (1)',
                f'GRANT USAGE ON SCHEMA "{schema}", "{staging}" TO inspector, dry_writer',
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO inspector',
                f'GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA "{schema}", "{staging}" TO dry_writer',
            ):
                await conn.exec_driver_sql(sql)
        writer_dsn = (
            make_url(pg_dsn)
            .set(username="dry_writer", password="dry-writer-canary-77")
            .render_as_string(hide_password=False)
        )
        store = PostgreSQLStagingStore(
            PostgreSQLStagingTarget(
                dsn=SecretStr(writer_dsn),
                principal="dry_writer",
                inspector_principal="inspector",
                target_id="main",
                namespace="dry_run",
                schema_name=staging,
            ),
            retention=retention,
            clock=clock,
        )
        staged = await staging_case(clock, retention)
        await store.begin(staged.spec)
        for batch in staged.batches:
            await store.stage(batch, staged.spec.context)
        run = await store.get_run(staged.spec.context)
        await store.seal(staged.spec.context, expected_revision=run.revision)
        inspector = (
            make_url(pg_dsn)
            .set(username="inspector", password="inspection-canary-47")
            .render_as_string(hide_password=False)
        )
        target = PostgreSQLTarget(
            dsn=SecretStr(inspector),
            target_id="main",
            include_schemas=(schema,),
            include_tables=tuple(
                (schema, name) for name in ("parents", "children", "keys")
            ),
        )
        catalog = await PostgreSQLDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        yield Database(engine, target, catalog, schema, staging)
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{staging}" CASCADE')
        await engine.dispose()


async def incoming(
    db: Database,
    *,
    operation: LoadOperation = LoadOperation.INSERT_ONLY,
    table: str = "parents",
    error_policy: str = "atomic",
) -> tuple[DryRunRequest, DryRunPolicy]:
    rows = [
        {
            "id": IntegerScalar(value=i),
            **({"amount": IntegerScalar(value=20)} if table != "keys" else {}),
        }
        for i in (1, 2)
    ]
    return await dry_run_case(
        db.catalog,
        rows,
        table_name=table,
        operation=operation,
        error_policy=error_policy,
    )


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_real_dry_run_leaves_target_staging_and_sequence_exactly_unchanged(
    dry_db: Database, operation: LoadOperation, caplog: pytest.LogCaptureFixture
) -> None:
    request, policy = await incoming(
        dry_db, operation=operation, error_policy="quarantine_invalid"
    )
    before = await dry_db.snapshot()
    with caplog.at_level("DEBUG", logger="sqlalchemy.engine"):
        plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert plan.ready
    assert plan.planned_inserts == 1
    assert plan.planned_updates == (1 if operation is LoadOperation.UPSERT else 0)
    assert plan.planned_quarantine == (0 if operation is LoadOperation.UPSERT else 1)
    assert await dry_db.snapshot() == before
    assert not caplog.records
    assert "inspection-canary-47" not in plan.model_dump_json()
    assert "dry-writer-canary-77" not in plan.model_dump_json()


async def test_key_only_upsert_skip(dry_db: Database) -> None:
    request, policy = await incoming(
        dry_db, table="keys", operation=LoadOperation.UPSERT
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert plan.ready and plan.planned_skips == 1 and plan.planned_inserts == 1
    assert await dry_db.snapshot() == before


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        (
            "REVOKE INSERT ON $schema.parents FROM dry_writer",
            "DRY_RUN_PERMISSION_DENIED",
        ),
        (
            "REVOKE SELECT ON $schema.parents FROM inspector",
            "DRY_RUN_PERMISSION_DENIED",
        ),
        ("GRANT CREATE ON SCHEMA $schema TO dry_writer", "DRY_RUN_WRITER_NOT_ALLOWED"),
        (
            "ALTER TABLE $schema.parents ADD COLUMN extra integer",
            "DATABASE_SCHEMA_DRIFT",
        ),
        (
            "ALTER TABLE $schema.parents ENABLE ROW LEVEL SECURITY",
            "DRY_RUN_DATABASE_SEMANTICS_UNVERIFIED",
        ),
    ],
)
async def test_fresh_permissions_and_schema_recheck(
    dry_db: Database, sql: str, code: str
) -> None:
    request, policy = await incoming(dry_db)
    await dry_db.sql(sql)
    before = await dry_db.snapshot()
    with pytest.raises(LoadError) as caught:
        await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert caught.value.error_code == code
    assert await dry_db.snapshot() == before


async def test_fk_missing_is_quarantine_without_target_change(dry_db: Database) -> None:
    request, policy = await dry_run_case(
        dry_db.catalog,
        [
            {
                "id": IntegerScalar(value=1),
                "parent": IntegerScalar(value=1),
                "amount": IntegerScalar(value=2),
            },
            {
                "id": IntegerScalar(value=2),
                "parent": IntegerScalar(value=999),
                "amount": IntegerScalar(value=3),
            },
        ],
        table_name="children",
        error_policy="quarantine_invalid",
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert plan.ready and plan.planned_inserts == 1 and plan.planned_quarantine == 1
    assert any("DB_FOREIGN_KEY" in step.codes for step in plan.steps)
    assert await dry_db.snapshot() == before


async def test_parent_is_ordered_before_child_even_for_reverse_mapping(
    dry_db: Database,
) -> None:
    request, policy = await dry_run_case(
        dry_db.catalog,
        [
            {
                "child_id": IntegerScalar(value=7),
                "child_parent": IntegerScalar(value=7),
                "child_amount": IntegerScalar(value=2),
                "parent_id": IntegerScalar(value=7),
                "parent_amount": IntegerScalar(value=3),
            }
        ],
        table_name="parents",
        targets={
            "child_id": ("children", "id"),
            "child_parent": ("children", "parent"),
            "child_amount": ("children", "amount"),
            "parent_id": ("parents", "id"),
            "parent_amount": ("parents", "amount"),
        },
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    ids = {t.name: t.table_id for s in dry_db.catalog.schemas for t in s.tables}
    assert plan.ready and plan.planned_inserts == 2
    assert plan.table_order == (ids["parents"], ids["children"])
    assert plan.steps[1].dependencies == (plan.steps[0].unit_id,)
    assert await dry_db.snapshot() == before


async def test_same_principal_rejected(dry_db: Database) -> None:
    request, policy = await incoming(dry_db)
    policy = policy.model_copy(update={"writer_principal": "inspector"})
    with pytest.raises(LoadError, match="DRY_RUN_WRITER_NOT_ALLOWED"):
        await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)


async def test_cancellation_closes_transaction_without_changes(
    dry_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.database import dry_run
    from structuraguard.database._dry_run_permissions import permissions

    original = permissions
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def paused(*args: object, **kwargs: object) -> None:
        entered.set()
        await hold.wait()

    monkeypatch.setattr(dry_run, "permissions", paused)
    request, policy = await incoming(dry_db)
    before = await dry_db.snapshot()
    planner = PostgreSQLDryRunPlanner(dry_db.target, policy=policy)
    task = asyncio.create_task(planner.plan(request))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(dry_run, "permissions", original)
    assert await dry_db.snapshot() == before
    async with dry_db.engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE application_name='structuraguard-inspection' AND state='idle in transaction'"
            )
        )
        assert count == 0
    assert (await planner.plan(request)).planned_quarantine == 1


@pytest.mark.parametrize("probe", ["insert", "sequence"])
async def test_server_enforces_read_only_even_if_internal_code_attempts_mutation(
    dry_db: Database, probe: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    from structuraguard.database import dry_run

    async def attempt(
        connection: AsyncConnection,
        catalog: DatabaseCatalog,
        policy: DryRunPolicy,
        mapping: object,
    ) -> None:
        assert await connection.scalar(text("SHOW transaction_read_only")) == "on"
        assert (
            await connection.scalar(text("SHOW transaction_isolation"))
            == "serializable"
        )
        if probe == "insert":
            await connection.execute(
                text(f'INSERT INTO "{dry_db.schema}".parents VALUES (:id, :amount)'),
                {"id": 91, "amount": 92},
            )
        else:
            await connection.execute(
                text("SELECT pg_catalog.nextval(CAST(:sequence AS regclass))"),
                {"sequence": f'"{dry_db.schema}".untouched'},
            )

    monkeypatch.setattr(dry_run, "permissions", attempt)
    await dry_db.sql("GRANT INSERT ON $schema.parents TO inspector")
    await dry_db.sql("GRANT USAGE ON SEQUENCE $schema.untouched TO inspector")
    request, policy = await incoming(dry_db)
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="DRY_RUN_DATABASE_READ_FAILED"):
        await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert await dry_db.snapshot() == before


@pytest.mark.parametrize("kind", ["column", "domain"])
async def test_unknown_default_blocks_ready_without_evaluating_sequence(
    dry_db: Database,
    kind: str,
) -> None:
    if kind == "column":
        await dry_db.sql(
            "ALTER TABLE $schema.parents ADD COLUMN token bigint DEFAULT nextval('$schema.untouched'::regclass)"
        )
    else:
        await dry_db.sql(
            "CREATE DOMAIN $schema.counter AS bigint DEFAULT nextval('$schema.untouched'::regclass)"
        )
        await dry_db.sql("ALTER TABLE $schema.parents ADD COLUMN token $schema.counter")
    # Existing rows получают default в явной тестовой миграции. Baseline снимается
    # после неё: dry-run не должен вычислить его повторно для planned rows.
    dry_db.catalog = await PostgreSQLDatabaseAdapter(dry_db.target).inspect(
        DatabaseInspectionRequest(
            target_id=dry_db.target.target_id,
            target_policy_fingerprint=dry_db.target.policy_fingerprint,
        )
    )
    request, policy = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert not plan.ready and "DRY_RUN_DEFAULT_UNVERIFIED" in plan.blockers
    assert await dry_db.snapshot() == before


async def test_unverified_check_cannot_be_quarantined_into_ready(
    dry_db: Database,
) -> None:
    await dry_db.sql("ALTER TABLE $schema.parents ADD CHECK (amount > 0)")
    dry_db.catalog = await PostgreSQLDatabaseAdapter(dry_db.target).inspect(
        DatabaseInspectionRequest(
            target_id=dry_db.target.target_id,
            target_policy_fingerprint=dry_db.target.policy_fingerprint,
        )
    )
    request, policy = await incoming(dry_db, error_policy="quarantine_invalid")
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert not plan.ready and "DB_CONSTRAINT_UNVERIFIED" in plan.blockers
    assert await dry_db.snapshot() == before


async def test_mapping_policy_is_revalidated_on_every_run(dry_db: Database) -> None:
    request, policy = await incoming(dry_db)
    policy = policy.model_copy(
        update={
            "mapping_policy": policy.mapping_policy.model_copy(
                update={"source_identity_allow": ()}
            )
        }
    )
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="DRY_RUN_MAPPING_REJECTED"):
        await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert await dry_db.snapshot() == before


async def test_invalid_parent_quarantines_complete_record_group(
    dry_db: Database,
) -> None:
    request, policy = await dry_run_case(
        dry_db.catalog,
        [
            {
                "child_id": IntegerScalar(value=7),
                "child_parent": IntegerScalar(value=1),
                "child_amount": IntegerScalar(value=2),
                "parent_id": IntegerScalar(value=1),
                "parent_amount": IntegerScalar(value=3),
            }
        ],
        table_name="parents",
        targets={
            "child_id": ("children", "id"),
            "child_parent": ("children", "parent"),
            "child_amount": ("children", "amount"),
            "parent_id": ("parents", "id"),
            "parent_amount": ("parents", "amount"),
        },
        error_policy="quarantine_invalid",
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert plan.ready and plan.planned_quarantine == 2 and plan.planned_inserts == 0
    assert any("DRY_RUN_DEPENDENCY_QUARANTINED" in s.codes for s in plan.steps)
    assert await dry_db.snapshot() == before


async def test_failure_diagnostics_do_not_expose_sql_parameters_or_credentials(
    dry_db: Database, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from sqlalchemy.exc import DBAPIError

    from structuraguard.database import dry_run

    async def fail(*args: object, **kwargs: object) -> None:
        raise DBAPIError(
            "SELECT sensitive_sql_canary",
            {"password": "parameter-canary-93"},
            RuntimeError("dsn=postgresql://login:secret-canary@host/db"),
        )

    monkeypatch.setattr(dry_run, "permissions", fail)
    request, policy = await incoming(dry_db)
    with caplog.at_level("DEBUG"), pytest.raises(LoadError) as caught:
        await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    import traceback

    diagnostic = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "parameter-canary-93" not in diagnostic
    assert "secret-canary" not in diagnostic
    assert "sensitive_sql_canary" not in diagnostic
    assert "inspection-canary-47" not in diagnostic


async def test_quoted_catalog_identifier_remains_an_identifier(
    dry_db: Database,
) -> None:
    from tests.fakes.mapping_validation import replan

    name = "odd'; DROP TABLE parents; --"
    async with dry_db.engine.begin() as conn:
        quote = conn.dialect.identifier_preparer.quote_identifier
        await conn.exec_driver_sql(
            f'CREATE TABLE "{dry_db.schema}".{quote(name)} (id integer PRIMARY KEY)'
        )
        await conn.exec_driver_sql(
            f'GRANT SELECT ON "{dry_db.schema}".{quote(name)} TO inspector'
        )
        await conn.exec_driver_sql(
            f'GRANT SELECT, INSERT, UPDATE ON "{dry_db.schema}".{quote(name)} TO dry_writer'
        )
    target = dry_db.target.model_copy(
        update={
            "include_tables": (*dry_db.target.include_tables, (dry_db.schema, name))
        }
    )
    catalog = await PostgreSQLDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    request, policy = await dry_run_case(
        catalog, [{"id": IntegerScalar(value=778899)}], table_name=name
    )
    # Ещё один валидный hash не должен открыть SQL из mapping identifiers.
    request = request.model_copy(
        update={"mapping": replan(request.mapping, plan_id="odd-plan")}
    )
    before = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(target, policy=policy).plan(request)
    assert plan.ready and plan.planned_inserts == 1
    assert "778899" not in plan.model_dump_json()
    assert await dry_db.snapshot() == before


async def test_upsert_classification_uses_the_same_snapshot_as_constraints(
    dry_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.contracts.constraint_validation import (
        ConstraintReadRequest,
        ConstraintReadResult,
    )
    from structuraguard.database._dry_run_reader import SnapshotReader

    original = SnapshotReader.read
    calls = 0

    async def concurrent(
        self: SnapshotReader,
        request: ConstraintReadRequest,
        *,
        catalog: DatabaseCatalog,
    ) -> ConstraintReadResult:
        nonlocal calls
        result = await original(self, request, catalog=catalog)
        calls += 1
        if calls == 1:
            # Только сторонний admin меняет БД; второй EXISTS планировщика обязан
            # остаться на прежнем snapshot, а не увидеть частично новое состояние.
            await dry_db.sql("INSERT INTO $schema.parents VALUES (2, 99)")
        return result

    monkeypatch.setattr(SnapshotReader, "read", concurrent)
    request, policy = await incoming(dry_db, operation=LoadOperation.UPSERT)
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=policy).plan(request)
    assert calls == 2
    assert plan.ready and plan.planned_updates == 1 and plan.planned_inserts == 1
