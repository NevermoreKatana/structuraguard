"""Batch caps компилируются до существующего SQL builder, без новых SQL controls."""

from uuid import UUID

import pytest
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_execution_plan import Keys

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.contracts.security import SecurityLimits, SecurityPolicy
from structuraguard.database import _load_queries
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare
from structuraguard.security import SecuritySession

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("extra", [0, 1])
async def test_db_row_boundary_splits_before_next_batch_accumulates(extra: int) -> None:
    _, _, catalog, _, _ = await case()
    incoming, preflight = await dry_run_case(
        catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=20)}
            for i in range(2, 4 + extra)
        ],
        table_name="orders",
    )
    prepared = await prepare(incoming)
    plan = await build_plan(prepared, catalog=catalog, policy=preflight, reader=Keys())
    run = SecuritySession(
        SecurityPolicy(limits=SecurityLimits(max_db_batch_rows=2)), run_id=UUID(int=1)
    )
    policy = run.load_policy(
        PostgreSQLLoadPolicy(preflight=preflight, write_tables=(("public", "orders"),))
    )
    batches = tuple(_load_queries.statements(prepared, plan, catalog, policy))
    assert [len(batch.unit_ids) for batch in batches] == ([2, 1] if extra else [2])
    assert len({unit for batch in batches for unit in batch.unit_ids}) == 2 + extra


@pytest.mark.parametrize("extra", [0, 1])
async def test_db_batch_bytes_exact_and_one_over_before_statement(
    extra: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, catalog, _, _ = await case()
    incoming, preflight = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="orders",
    )
    prepared = await prepare(incoming)
    plan = await build_plan(prepared, catalog=catalog, policy=preflight, reader=Keys())
    size = len(prepared.data.records[0].canonical_json().encode())
    run = SecuritySession(
        SecurityPolicy(limits=SecurityLimits(max_db_batch_bytes=size - extra)),
        run_id=UUID(int=1),
    )
    policy = run.load_policy(
        PostgreSQLLoadPolicy(preflight=preflight, write_tables=(("public", "orders"),))
    )

    async def build() -> int:
        return len(tuple(_load_queries.statements(prepared, plan, catalog, policy)))

    if extra:

        def forbidden(*args: object, **kwargs: object) -> None:
            pytest.fail("SQL statement создан после нарушения batch limit")

        monkeypatch.setattr(_load_queries, "_relation", forbidden)
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await run.call(build)
        assert run.events
    else:
        assert await run.call(build) == 1
