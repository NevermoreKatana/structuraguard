"""SQLite проверяет общую Core INSERT часть; PostgreSQL semantics — в integration."""

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_execution_plan import Keys

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import LoadLedgerPolicy, PostgreSQLLoadPolicy
from structuraguard.database._load_queries import statements
from structuraguard.loading.groups import dependency_groups
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("size", [1, 3, 20])
@pytest.mark.parametrize("quarantine", [False, True])
async def test_core_insert_batches_preserve_dataset_in_sqlite(
    size: int, quarantine: bool
) -> None:
    _, _, catalog, _, _ = await case()
    snapshot, preflight = await dry_run_case(
        catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=i * 2)}
            for i in range(1 if quarantine else 2, 13)
        ],
        table_name="orders",
        batch_size=2,
        error_policy="quarantine_invalid" if quarantine else "atomic",
    )
    prepared = await prepare(snapshot)
    plan = await build_plan(prepared, catalog=catalog, policy=preflight, reader=Keys())
    engine = create_engine("sqlite://")
    metadata = MetaData()
    table = Table(
        "orders",
        metadata,
        Column("id", Integer(), primary_key=True),
        Column("amount", Integer()),
        schema="public",
    )
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS public")
            metadata.create_all(connection)
            returned = 0
            policy = PostgreSQLLoadPolicy(
                preflight=preflight,
                write_tables=(("public", "orders"),),
                batch_size=size,
                max_parameters=7,
                ledger=LoadLedgerPolicy(namespace="contract") if quarantine else None,
            )
            if quarantine:
                groups = dependency_groups(prepared, plan, 1000)
                for group in groups:
                    for batch in statements(
                        prepared, plan, catalog, policy, unit_ids=frozenset(group)
                    ):
                        returned += len(connection.execute(batch.statement).all())
            else:
                for batch in statements(prepared, plan, catalog, policy):
                    returned += len(connection.execute(batch.statement).all())
            assert returned == 11
            assert tuple(
                tuple(row)
                for row in connection.execute(select(table).order_by(table.c.id))
            ) == tuple((i, i * 2) for i in range(2, 13))
    finally:
        engine.dispose()
