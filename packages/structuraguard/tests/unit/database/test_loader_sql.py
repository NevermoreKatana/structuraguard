"""Bulk SQL сохраняет все units и не интерполирует значения."""

import pytest
from sqlalchemy import create_mock_engine
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_execution_plan import Keys

from structuraguard.contracts.common import IntegerScalar, LoadOperation
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.database._load_queries import statements
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("size", [1, 2, 4])
async def test_bulk_chunks_cover_every_unit_once_with_parameterized_values(
    size: int,
) -> None:
    _, _, catalog, _, _ = await case()
    incoming, policy = await dry_run_case(
        catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=987654321)}
            for i in range(2, 9)
        ],
        table_name="orders",
        operation=LoadOperation.UPSERT,
    )
    prepared = await prepare(incoming)
    plan = await build_plan(prepared, catalog=catalog, policy=policy, reader=Keys())
    batches = tuple(
        statements(
            prepared,
            plan,
            catalog,
            PostgreSQLLoadPolicy(
                preflight=policy, write_tables=(("public", "orders"),), batch_size=size
            ),
        )
    )
    assert [len(b.unit_ids) for b in batches] == [size] * (7 // size) + (
        [7 % size] if 7 % size else []
    )
    assert len({uid for b in batches for uid in b.unit_ids}) == 7
    for batch in batches:
        dialect = create_mock_engine(
            "postgresql://", lambda *args, **kwargs: None
        ).dialect
        compiled = batch.statement.compile(dialect=dialect)
        assert "987654321" not in str(compiled)
        assert 987654321 in compiled.params.values()
        assert "ON CONFLICT (id) DO UPDATE SET amount = excluded.amount" in str(
            compiled
        )
        assert "RETURNING" in str(compiled)
