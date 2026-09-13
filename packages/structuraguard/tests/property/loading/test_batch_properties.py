"""Независимый SQLite oracle для произвольных source/SQL batch boundaries."""

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_execution_plan import Keys

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.database._load_queries import statements
from structuraguard.exceptions import LoadError
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare


@settings(max_examples=30, deadline=None, derandomize=True)
@given(
    identifiers=st.lists(st.integers(2, 10000), min_size=1, max_size=20, unique=True),
    source_size=st.integers(1, 9),
    sql_size=st.integers(1, 9),
    max_parameters=st.integers(2, 19),
)
def test_arbitrary_batch_boundaries_preserve_rows_without_duplicates_or_omissions(
    identifiers: list[int], source_size: int, sql_size: int, max_parameters: int
) -> None:
    async def run() -> None:
        _, _, catalog, _, _ = await case()
        rows = [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=-i)}
            for i in identifiers
        ]
        snapshot, preflight = await dry_run_case(
            catalog, rows, table_name="orders", batch_size=source_size
        )
        prepared = await prepare(snapshot)
        plan = await build_plan(
            prepared, catalog=catalog, policy=preflight, reader=Keys()
        )
        assert plan.ready and plan.planned_inserts == len(identifiers)
        policy = PostgreSQLLoadPolicy(
            preflight=preflight,
            write_tables=(("public", "orders"),),
            batch_size=sql_size,
            max_parameters=max_parameters,
        )
        engine = create_engine("sqlite://")
        metadata = MetaData()
        table = Table(
            "orders",
            metadata,
            Column("id", Integer, primary_key=True),
            Column("amount", Integer),
            schema="public",
        )
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS public")
                metadata.create_all(connection)
                if max_parameters < 3:
                    # Две колонки и параметризованный RETURNING требуют три binds.
                    with pytest.raises(LoadError, match="SECURITY_LIMIT_EXCEEDED"):
                        tuple(statements(prepared, plan, catalog, policy))
                    assert connection.execute(select(table)).all() == []
                    return
                count = 0
                for batch in statements(prepared, plan, catalog, policy):
                    assert len(batch.unit_ids) <= sql_size
                    assert len(batch.statement.compile().params) <= max_parameters
                    count += len(connection.execute(batch.statement).all())
                assert count == len(identifiers)
                assert [
                    tuple(row)
                    for row in connection.execute(select(table).order_by(table.c.id))
                ] == [(i, -i) for i in sorted(identifiers)]
        finally:
            engine.dispose()

    asyncio.run(run())
