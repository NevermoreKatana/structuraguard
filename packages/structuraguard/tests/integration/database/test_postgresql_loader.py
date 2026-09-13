"""Настоящие insert/upsert: атомарный parent/child и полный bulk scope."""

from collections.abc import Iterator, Sequence

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.staging import StagingClock
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db

from structuraguard.contracts.common import (
    IntegerScalar,
    LoadOperation,
    NormalizedScalar,
    NullScalar,
)
from structuraguard.contracts.database import (
    DatabaseInspectionRequest,
)
from structuraguard.contracts.loading import (
    DryRunPolicy,
    DryRunRequest,
    PostgreSQLLoadPolicy,
    ServerValuePermission,
)
from structuraguard.contracts.staging import StagingRetentionPolicy, StagingRunStatus
from structuraguard.database import PostgreSQLDatabaseAdapter
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.exceptions import LoadError
from structuraguard.stores import (
    MemoryStagingStore,
    PostgreSQLStagingStore,
    PostgreSQLStagingTarget,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


def writer(db: Database, pg_dsn: str) -> PostgreSQLWriterTarget:
    dsn = (
        make_url(pg_dsn)
        .set(username="dry_writer", password="dry-writer-canary-77")
        .render_as_string(hide_password=False)
    )
    return PostgreSQLWriterTarget(
        inspection=db.target, dsn=SecretStr(dsn), principal="dry_writer"
    )


def policy_for(
    db: Database,
    snapshot: DryRunRequest,
    preflight: DryRunPolicy,
    *,
    size: int = 100,
    parameters: int = 30000,
    server_values: tuple[ServerValuePermission, ...] = (),
) -> PostgreSQLLoadPolicy:
    selected = {m.target.table_id for m in snapshot.mapping.mappings}
    writes = tuple(
        (t.schema_name, t.name)
        for s in db.catalog.schemas
        for t in s.tables
        if t.table_id in selected
    )
    return PostgreSQLLoadPolicy(
        preflight=preflight,
        write_tables=writes,
        batch_size=size,
        max_parameters=parameters,
        server_values=server_values,
    )


async def contents(db: Database, table: str) -> tuple[tuple[object, ...], ...]:
    async with db.engine.connect() as connection:
        quote = connection.dialect.identifier_preparer.quote_identifier
        return tuple(
            tuple(r)
            for r in (
                await connection.exec_driver_sql(
                    f"SELECT * FROM {quote(db.schema)}.{quote(table)} ORDER BY 1,2"
                )
            ).all()
        )


async def refresh(db: Database, names: Sequence[str] = ()) -> None:
    db.target = db.target.model_copy(
        update={
            "include_tables": (
                *db.target.include_tables,
                *((db.schema, n) for n in names),
            )
        }
    )
    db.catalog = await PostgreSQLDatabaseAdapter(db.target).inspect(
        DatabaseInspectionRequest(
            target_id=db.target.target_id,
            target_policy_fingerprint=db.target.policy_fingerprint,
        )
    )


async def test_insert_parent_child_uses_graph_order_and_postgresql_staging(
    dry_db: Database, pg_dsn: str
) -> None:
    db = dry_db
    snapshot, preflight = await dry_run_case(
        db.catalog,
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
    target, clock = writer(db, pg_dsn), StagingClock()
    store = PostgreSQLStagingStore(
        PostgreSQLStagingTarget(
            dsn=target.dsn,
            principal="dry_writer",
            inspector_principal="inspector",
            target_id="main",
            namespace="loader",
            schema_name=db.staging,
        ),
        retention=StagingRetentionPolicy(),
        clock=clock,
    )
    request = await sealed_input(snapshot, store, clock)
    result = await PostgreSQLLoader(
        target,
        policy=policy_for(db, snapshot, preflight, size=1),
        staging=store,
        clock=clock,
    ).execute(request)
    assert result.inserted == 2 and result.updated == 0
    assert await contents(db, "parents") == ((1, 10), (7, 3))
    assert await contents(db, "children") == ((7, 7, 2),)
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.COMMITTED


async def test_upsert_updates_existing_and_inserts_new_without_duplicate(
    dry_db: Database, pg_dsn: str
) -> None:
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=20)}
            for i in (1, 2)
        ],
        table_name="parents",
        operation=LoadOperation.UPSERT,
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight, size=1),
        staging=store,
        clock=clock,
    )
    result = await loader.execute(request)
    assert (result.inserted, result.updated, result.skipped) == (1, 1, 0)
    assert await contents(dry_db, "parents") == ((1, 20), (2, 20))
    with pytest.raises(LoadError, match="LOAD_STAGING_NOT_SEALED"):
        await loader.execute(request)
    again = await loader.execute(
        await sealed_input(snapshot, store, clock, run_id="load-2")
    )
    assert (again.inserted, again.updated) == (0, 2)
    assert await contents(dry_db, "parents") == ((1, 20), (2, 20))


@pytest.mark.parametrize("kind", ["existing", "across_batches"])
async def test_duplicate_key_rejects_whole_run(
    dry_db: Database, pg_dsn: str, kind: str
) -> None:
    values = (2, 1) if kind == "existing" else (2, 2)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=20)}
            for i in values
        ],
        table_name="parents",
        batch_size=1,
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="LOAD_DUPLICATE_KEY"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight, size=1),
            staging=store,
            clock=clock,
        ).execute(request)
    assert await dry_db.snapshot() == before
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.ROLLED_BACK
    assert (
        len(await store.read_records(request.staging_context, offset=0, limit=10)) == 2
    )


@pytest.mark.parametrize("size", [1, 4, 20])
async def test_bulk_boundaries_preserve_all_rows_and_missing_null_shapes(
    dry_db: Database, pg_dsn: str, size: int
) -> None:
    rows: list[dict[str, NormalizedScalar]] = [
        {
            "id": IntegerScalar(value=i),
            **(
                {"amount": IntegerScalar(value=i * 2)}
                if i % 3 == 0
                else {"amount": NullScalar()}
                if i % 3 == 1
                else {}
            ),
        }
        for i in range(2, 19)
    ]
    snapshot, preflight = await dry_run_case(
        dry_db.catalog, rows, table_name="parents", batch_size=3
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight, size=size, parameters=7),
        staging=store,
        clock=clock,
    ).execute(request)
    assert result.inserted == 17
    assert await contents(dry_db, "parents") == (
        (1, 10),
        *tuple((i, i * 2 if i % 3 == 0 else None) for i in range(2, 19)),
    )


async def test_lookup_only_parent_needs_select_without_update(
    dry_db: Database, pg_dsn: str
) -> None:
    await dry_db.sql("REVOKE INSERT, UPDATE ON $schema.parents FROM dry_writer")
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "id": IntegerScalar(value=2),
                "parent": IntegerScalar(value=1),
                "amount": IntegerScalar(value=8),
            }
        ],
        table_name="children",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    ).execute(request)
    assert result.inserted == 1
    assert await contents(dry_db, "parents") == ((1, 10),)
    assert await contents(dry_db, "children") == ((2, 1, 8),)


@pytest.mark.parametrize("key_kind", ["primary", "natural"])
async def test_composite_upsert_matches_full_confirmed_key(
    dry_db: Database, pg_dsn: str, key_kind: str
) -> None:
    from tests.fakes.mapping_validation import replan

    from structuraguard.contracts.mapping_rules import MappingIdentity

    constraint = (
        "PRIMARY KEY (tenant, code)"
        if key_kind == "primary"
        else "UNIQUE (tenant, code)"
    )
    await dry_db.sql(
        f"CREATE TABLE $schema.composite (tenant integer NOT NULL, code integer NOT NULL, amount integer, {constraint})"
    )
    await dry_db.sql("GRANT SELECT ON $schema.composite TO inspector")
    await dry_db.sql("GRANT SELECT, INSERT, UPDATE ON $schema.composite TO dry_writer")
    await dry_db.sql("INSERT INTO $schema.composite VALUES (1, 7, 10), (2, 7, 11)")
    await refresh(dry_db, ("composite",))
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "tenant": IntegerScalar(value=1),
                "code": IntegerScalar(value=7),
                "amount": IntegerScalar(value=20),
            },
            {
                "tenant": IntegerScalar(value=3),
                "code": IntegerScalar(value=7),
                "amount": IntegerScalar(value=30),
            },
        ],
        table_name="composite",
        operation=LoadOperation.UPSERT,
        batch_size=1,
    )
    if key_kind == "natural":
        table = next(
            t for s in dry_db.catalog.schemas for t in s.tables if t.name == "composite"
        )
        columns = {c.name: c.column_id for c in table.columns}
        identity = MappingIdentity(
            table_id=table.table_id,
            kind="natural_key",
            column_ids=(columns["tenant"], columns["code"]),
        )
        preflight = preflight.model_copy(
            update={
                "mapping_policy": preflight.mapping_policy.model_copy(
                    update={"natural_keys": (identity,)}
                )
            }
        )
        snapshot = snapshot.model_copy(
            update={"mapping": replan(snapshot.mapping, identities=(identity,))}
        )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight, size=2),
        staging=store,
        clock=clock,
    ).execute(request)
    assert (result.inserted, result.updated) == (1, 1)
    assert await contents(dry_db, "composite") == ((1, 7, 20), (2, 7, 11), (3, 7, 30))


async def test_composite_fk_lookup_preserves_catalog_key_order(
    dry_db: Database, pg_dsn: str
) -> None:
    await dry_db.sql(
        "CREATE TABLE $schema.composite_parent (a integer, b integer, PRIMARY KEY (b,a))"
    )
    await dry_db.sql(
        "CREATE TABLE $schema.composite_child (id integer PRIMARY KEY, a integer, b integer, FOREIGN KEY(b,a) REFERENCES $schema.composite_parent(b,a))"
    )
    await dry_db.sql("INSERT INTO $schema.composite_parent VALUES (4,9)")
    await dry_db.sql(
        "GRANT SELECT ON $schema.composite_parent,$schema.composite_child TO inspector,dry_writer"
    )
    await dry_db.sql("GRANT INSERT,UPDATE ON $schema.composite_child TO dry_writer")
    await refresh(dry_db, ("composite_parent", "composite_child"))
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "id": IntegerScalar(value=1),
                "a": IntegerScalar(value=4),
                "b": IntegerScalar(value=9),
            }
        ],
        table_name="composite_child",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    ).execute(await sealed_input(snapshot, store, clock))
    assert result.inserted == 1
    assert await contents(dry_db, "composite_child") == ((1, 4, 9),)


async def test_parent_in_later_source_batch_is_loaded_first(
    dry_db: Database, pg_dsn: str
) -> None:
    from tests.fakes.profiling import make_record, record_stream

    child = make_record(
        (
            (
                "child",
                {
                    "id": IntegerScalar(value=2),
                    "parent": IntegerScalar(value=8),
                    "amount": IntegerScalar(value=20),
                },
            ),
        ),
        ordinal=0,
    )
    parent = make_record(
        (
            (
                "parent",
                {"id": IntegerScalar(value=8), "amount": IntegerScalar(value=30)},
            ),
        ),
        ordinal=1,
    )
    batches = tuple([b async for b in record_stream(((child,), (parent,)))])
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        (),
        table_name="parents",
        supplied_batches=batches,
        targets={
            "child.id": ("children", "id"),
            "child.parent": ("children", "parent"),
            "child.amount": ("children", "amount"),
            "parent.id": ("parents", "id"),
            "parent.amount": ("parents", "amount"),
        },
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight, size=1),
        staging=store,
        clock=clock,
    ).execute(await sealed_input(snapshot, store, clock))
    assert result.inserted == 2
    assert await contents(dry_db, "children") == ((2, 8, 20),)
    assert await contents(dry_db, "parents") == ((1, 10), (8, 30))


@pytest.mark.parametrize("permitted", [False, True])
@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_generated_column_is_omitted_and_requires_explicit_server_permission(
    dry_db: Database, pg_dsn: str, permitted: bool, operation: LoadOperation
) -> None:
    from structuraguard.contracts._base import canonical_sha256_value
    from structuraguard.contracts.database import CatalogColumnRef

    await dry_db.sql(
        "ALTER TABLE $schema.parents ADD COLUMN total integer GENERATED ALWAYS AS (amount * 2) STORED"
    )
    await refresh(dry_db)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "id": IntegerScalar(
                    value=1 if operation is LoadOperation.UPSERT else 2
                ),
                "amount": IntegerScalar(value=20),
            }
        ],
        table_name="parents",
        operation=operation,
    )
    table = next(
        t for s in dry_db.catalog.schemas for t in s.tables if t.name == "parents"
    )
    column = next(c for c in table.columns if c.name == "total")
    assert column.inspection is not None
    grants = (
        (
            ServerValuePermission(
                column=CatalogColumnRef(
                    table_id=table.table_id, column_id=column.column_id
                ),
                metadata_fingerprint=canonical_sha256_value(
                    column.inspection.canonical_json()
                ),
                evaluation_allowed=True,
            ),
        )
        if permitted
        else ()
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight, server_values=grants),
        staging=store,
        clock=clock,
    )
    request = await sealed_input(snapshot, store, clock)
    if permitted:
        result = await loader.execute(request)
        if operation is LoadOperation.UPSERT:
            assert (result.inserted, result.updated) == (0, 1)
            assert await contents(dry_db, "parents") == ((1, 20, 40),)
        else:
            assert (result.inserted, result.updated) == (1, 0)
            assert await contents(dry_db, "parents") == ((1, 10, 20), (2, 20, 40))
    else:
        with pytest.raises(LoadError, match="LOAD_VALIDATION_REJECTED"):
            await loader.execute(request)
        assert await contents(dry_db, "parents") == ((1, 10, 20),)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            "ALTER TABLE $schema.parents ADD COLUMN changed integer",
            "DATABASE_SCHEMA_DRIFT",
        ),
        (
            "REVOKE INSERT ON $schema.parents FROM dry_writer",
            "DRY_RUN_PERMISSION_DENIED",
        ),
    ],
)
async def test_execution_rechecks_schema_and_grants_after_successful_dry_run(
    dry_db: Database, pg_dsn: str, change: str, code: str
) -> None:
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    )
    assert (await loader.dry_run(snapshot)).ready
    request = await sealed_input(snapshot, store, clock)
    await dry_db.sql(change)
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match=code):
        await loader.execute(request)
    assert await dry_db.snapshot() == before
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.ROLLED_BACK


async def test_unconfirmed_identity_cannot_select_upsert_target(
    dry_db: Database, pg_dsn: str
) -> None:
    from tests.fakes.mapping_validation import replan

    from structuraguard.contracts.mapping_rules import MappingIdentity

    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
        operation=LoadOperation.UPSERT,
    )
    ref = next(
        m.target for m in snapshot.mapping.mappings if m.source.field_name == "amount"
    )
    snapshot = snapshot.model_copy(
        update={
            "mapping": replan(
                snapshot.mapping,
                identities=(
                    MappingIdentity(
                        table_id=ref.table_id,
                        kind="explicit",
                        column_ids=(ref.column_id,),
                    ),
                ),
            )
        }
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="DRY_RUN_MAPPING_REJECTED"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight),
            staging=store,
            clock=clock,
        ).execute(await sealed_input(snapshot, store, clock))
    assert await dry_db.snapshot() == before


async def test_late_sql_duplicate_rolls_back_previous_bulk(
    dry_db: Database, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import Column, Integer, MetaData, Table, literal
    from sqlalchemy.dialects.postgresql import insert

    from structuraguard.contracts.database import DatabaseCatalog
    from structuraguard.contracts.loading import DryRunExecutionPlan
    from structuraguard.database import _load_transaction
    from structuraguard.database._load_queries import WriteBatch, statements
    from structuraguard.loading.projection import Prepared

    def late_duplicate(
        prepared: Prepared,
        plan: DryRunExecutionPlan,
        catalog: DatabaseCatalog,
        policy: PostgreSQLLoadPolicy,
    ) -> Iterator[WriteBatch]:
        yield from statements(prepared, plan, catalog, policy)
        table = Table(
            "parents",
            MetaData(),
            Column("id", Integer()),
            Column("amount", Integer()),
            schema=dry_db.schema,
        )
        yield WriteBatch(
            ("fault",), insert(table).values(id=1, amount=999).returning(literal(1))
        )

    monkeypatch.setattr(_load_transaction, "statements", late_duplicate)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="LOAD_DUPLICATE_KEY"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight),
            staging=store,
            clock=clock,
        ).execute(await sealed_input(snapshot, store, clock))
    assert await dry_db.snapshot() == before


@pytest.mark.parametrize("valid", [True, False])
async def test_mapped_parent_checks_actual_values_and_source_link(
    dry_db: Database, pg_dsn: str, valid: bool
) -> None:
    from tests.fakes.mapping_validation import replan

    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "parent_id": IntegerScalar(value=8),
                "parent_amount": IntegerScalar(value=30),
                "child_id": IntegerScalar(value=2),
                "child_parent": IntegerScalar(value=8 if valid else 1),
                "child_amount": IntegerScalar(value=20),
            }
        ],
        table_name="parents",
        targets={
            "parent_id": ("parents", "id"),
            "parent_amount": ("parents", "amount"),
            "child_id": ("children", "id"),
            "child_parent": ("children", "parent"),
            "child_amount": ("children", "amount"),
        },
    )
    relation = (snapshot.mapping.relations or ())[0]
    parent_ref = next(
        m.source
        for m in snapshot.mapping.mappings
        if m.source.field_name == "parent_id"
    )
    relation = relation.model_copy(
        update={"strategy": "mapped_parent", "parent_sources": (parent_ref,)}
    )
    snapshot = snapshot.model_copy(
        update={"mapping": replan(snapshot.mapping, relations=(relation,))}
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    )
    if valid:
        assert (await loader.execute(request)).inserted == 2
    else:
        before = await dry_db.snapshot()
        with pytest.raises(LoadError, match="LOAD_FK_PARENT_MISMATCH"):
            await loader.execute(request)
        assert await dry_db.snapshot() == before


async def test_cancellation_after_dml_rolls_back_and_closes_run(
    dry_db: Database, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from structuraguard.database import _load_transaction
    from structuraguard.database._dry_run_permissions import permissions

    entered = asyncio.Event()
    hold = asyncio.Event()
    calls = 0

    async def pause_after_dml(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            await hold.wait()

    monkeypatch.setattr(_load_transaction, "permissions", pause_after_dml)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    )
    before = await dry_db.snapshot()
    task = asyncio.create_task(loader.execute(request))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(_load_transaction, "permissions", permissions)
    assert await dry_db.snapshot() == before
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.CANCELLED


async def test_lost_commit_response_marks_unknown_and_never_replays(
    dry_db: Database, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncConnection

    original = AsyncConnection.commit

    async def lost(self: AsyncConnection) -> None:
        await original(self)
        raise OSError("commit-response-canary")

    monkeypatch.setattr(AsyncConnection, "commit", lost)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    )
    with pytest.raises(LoadError, match="LOAD_OUTCOME_UNKNOWN") as caught:
        await loader.execute(request)
    assert "commit-response-canary" not in str(caught.value)
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.UNKNOWN
    assert await contents(dry_db, "parents") == ((1, 10), (2, 20))
    with pytest.raises(LoadError):
        await loader.execute(request)
    assert await contents(dry_db, "parents") == ((1, 10), (2, 20))


async def test_writer_owning_function_is_rejected_without_target_change(
    dry_db: Database, pg_dsn: str
) -> None:
    await dry_db.sql(
        "CREATE FUNCTION $schema.owned() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
    )
    await dry_db.sql("ALTER FUNCTION $schema.owned() OWNER TO dry_writer")
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="DRY_RUN_WRITER_NOT_ALLOWED"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight),
            staging=store,
            clock=clock,
        ).execute(await sealed_input(snapshot, store, clock))
    assert await dry_db.snapshot() == before


async def test_loader_dry_run_never_claims_staging_or_uses_writer(
    dry_db: Database, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.database import _load_transaction

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("dry-run открыл writer engine")

    monkeypatch.setattr(_load_transaction, "writer_engine", forbidden)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    run = await store.get_run(request.staging_context)
    before = await dry_db.snapshot()
    plan = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    ).dry_run(snapshot)
    assert plan.ready and plan.planned_inserts == 1
    assert await store.get_run(request.staging_context) == run
    assert await dry_db.snapshot() == before


async def test_quoted_identifiers_and_string_parameters_are_data(
    dry_db: Database, pg_dsn: str, caplog: pytest.LogCaptureFixture
) -> None:
    from structuraguard.contracts.common import StringScalar

    name = "odd'; DROP TABLE parents; --"
    async with dry_db.engine.begin() as connection:
        quote = connection.dialect.identifier_preparer.quote_identifier
        relation = f"{quote(dry_db.schema)}.{quote(name)}"
        await connection.exec_driver_sql(
            f"CREATE TABLE {relation} (code text NOT NULL, payload text)"
        )
        # Catalog учитывает index collation; custom column collation отвергается.
        await connection.exec_driver_sql(
            f'CREATE UNIQUE INDEX quoted_code_key ON {relation} (code COLLATE "C")'
        )
        await connection.exec_driver_sql(f"GRANT SELECT ON {relation} TO inspector")
        await connection.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE ON {relation} TO dry_writer"
        )
    await refresh(dry_db, (name,))
    canary = "value'; DROP TABLE parents; -- password=fixture-write-canary-99"
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "code": StringScalar(value=canary),
                "payload": StringScalar(value="restricted-payload-62"),
            }
        ],
        table_name=name,
        semantic_type="string",
        operation=LoadOperation.UPSERT,
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    loader = PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    )
    with caplog.at_level("DEBUG", logger="sqlalchemy.engine"):
        result = await loader.execute(request)
        update_result = await loader.execute(
            await sealed_input(snapshot, store, clock, run_id="text-update")
        )
    assert result.inserted == 1
    assert (update_result.inserted, update_result.updated) == (0, 1)
    assert "fixture-write-canary-99" not in result.model_dump_json() + caplog.text
    assert "restricted-payload-62" not in result.model_dump_json() + caplog.text
    assert "dry-writer-canary-77" not in repr(result) + caplog.text
    assert await contents(dry_db, name) == ((canary, "restricted-payload-62"),)
    assert await contents(dry_db, "parents") == ((1, 10),)


@pytest.mark.parametrize("failure_kind", ["staging", "runtime"])
async def test_commit_remains_committed_when_staging_finalize_fails(
    dry_db: Database,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
) -> None:
    from structuraguard.contracts.database import StagingContext
    from structuraguard.contracts.staging import StagingRun
    from structuraguard.exceptions import StagingError

    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="parents",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    original = store.transition

    async def fail(
        context: StagingContext, *, expected_revision: int, status: StagingRunStatus
    ) -> StagingRun:
        if status is StagingRunStatus.COMMITTED:
            if failure_kind == "runtime":
                raise RuntimeError("staging-finalize-secret-canary")
            raise StagingError(
                error_code="STAGING_STORAGE_FAILED", message="STAGING_STORAGE_FAILED"
            )
        return await original(
            context, expected_revision=expected_revision, status=status
        )

    monkeypatch.setattr(store, "transition", fail)
    result = await PostgreSQLLoader(
        writer(dry_db, pg_dsn),
        policy=policy_for(dry_db, snapshot, preflight),
        staging=store,
        clock=clock,
    ).execute(request)
    assert result.inserted == 1 and result.transaction_outcome == "committed"
    assert result.warnings == ("LOAD_STAGING_FINALIZE_FAILED",)
    assert (
        "staging-finalize-secret-canary" not in result.model_dump_json() + caplog.text
    )
    assert await contents(dry_db, "parents") == ((1, 10), (2, 20))
    assert (
        await store.get_run(request.staging_context)
    ).status is StagingRunStatus.EXECUTING


async def test_fk_query_budget_vetoes_write(dry_db: Database, pg_dsn: str) -> None:
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [
            {
                "id": IntegerScalar(value=2),
                "parent": IntegerScalar(value=1),
                "amount": IntegerScalar(value=20),
            }
        ],
        table_name="children",
    )
    preflight = preflight.model_copy(
        update={
            "read_policy": preflight.read_policy.model_copy(
                update={"chunk_size": 1, "max_queries": 1}
            )
        }
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    before = await dry_db.snapshot()
    with pytest.raises(LoadError, match="SECURITY_LIMIT_EXCEEDED"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight),
            staging=store,
            clock=clock,
        ).execute(await sealed_input(snapshot, store, clock))
    assert await dry_db.snapshot() == before
