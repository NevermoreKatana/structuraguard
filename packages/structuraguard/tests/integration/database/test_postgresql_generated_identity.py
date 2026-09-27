"""INSERT с PK identity не требует входного ключа и не вычисляет его в dry-run."""

import pytest
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.staging import StagingClock
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_loader import (
    contents,
    policy_for,
    refresh,
    writer,
)

from structuraguard.contracts.common import IntegerScalar, LoadOperation
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.database.dry_run import PostgreSQLDryRunPlanner
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.exceptions import LoadError
from structuraguard.stores import MemoryStagingStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


async def create_target(db: Database, generation: str, *, extra: bool = False) -> None:
    await db.sql(
        "CREATE TABLE $schema.generated_records ("
        f"id integer GENERATED {generation} AS IDENTITY PRIMARY KEY, "
        "amount integer NOT NULL" + (", extra integer DEFAULT 7" if extra else "") + ")"
    )
    await db.sql("GRANT SELECT ON $schema.generated_records TO inspector")
    await db.sql(
        "GRANT SELECT, INSERT, UPDATE ON $schema.generated_records TO dry_writer"
    )
    await refresh(db, ("generated_records",))


async def sequence_state(db: Database) -> tuple[object, ...]:
    async with db.engine.connect() as connection:
        result = await connection.exec_driver_sql(
            f'SELECT last_value, is_called FROM "{db.schema}".generated_records_id_seq'
        )
        return tuple(result.one())


@pytest.mark.parametrize("generation", ["ALWAYS", "BY DEFAULT"])
async def test_insert_generated_identity_preserves_duplicate_values_and_read_only_plan(
    dry_db: Database, pg_dsn: str, generation: str
) -> None:
    await create_target(dry_db, generation)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"amount": IntegerScalar(value=7)}, {"amount": IntegerScalar(value=7)}],
        table_name="generated_records",
    )
    before_sequence = await sequence_state(dry_db)
    before_database = await dry_db.snapshot()
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=preflight).plan(snapshot)
    assert plan.ready, plan.blockers
    assert (plan.planned_inserts, plan.planned_updates) == (2, 0)
    assert all(
        not set(step.identity_column_ids).intersection(step.column_ids)
        for step in plan.steps
    )
    assert await sequence_state(dry_db) == before_sequence
    assert await dry_db.snapshot() == before_database
    assert await contents(dry_db, "generated_records") == ()

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
    assert (result.inserted, result.updated, result.skipped) == (2, 0, 0)
    assert await contents(dry_db, "generated_records") == ((1, 7), (2, 7))


async def test_generated_identity_does_not_authorize_other_defaults(
    dry_db: Database, pg_dsn: str
) -> None:
    await create_target(dry_db, "ALWAYS", extra=True)
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"amount": IntegerScalar(value=7)}],
        table_name="generated_records",
    )
    before = await sequence_state(dry_db)
    plan = await PostgreSQLDryRunPlanner(dry_db.target, policy=preflight).plan(snapshot)
    assert not plan.ready
    assert "DRY_RUN_DEFAULT_UNVERIFIED" in plan.blockers
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    with pytest.raises(LoadError, match="LOAD_VALIDATION_REJECTED"):
        await PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy_for(dry_db, snapshot, preflight),
            staging=store,
            clock=clock,
        ).execute(request)
    assert await sequence_state(dry_db) == before
    assert await contents(dry_db, "generated_records") == ()


async def test_generated_identity_does_not_authorize_upsert(dry_db: Database) -> None:
    await create_target(dry_db, "ALWAYS")
    snapshot, preflight = await dry_run_case(
        dry_db.catalog,
        [{"amount": IntegerScalar(value=7)}],
        table_name="generated_records",
        operation=LoadOperation.UPSERT,
    )
    before = await sequence_state(dry_db)
    with pytest.raises(LoadError, match="DRY_RUN_MAPPING_REJECTED"):
        await PostgreSQLDryRunPlanner(dry_db.target, policy=preflight).plan(snapshot)
    assert await sequence_state(dry_db) == before
    assert await contents(dry_db, "generated_records") == ()
