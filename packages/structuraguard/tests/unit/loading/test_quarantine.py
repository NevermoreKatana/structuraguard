"""Quarantine groups замкнуты по dependencies и ограничены work budget."""

import pytest
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_execution_plan import Keys

from structuraguard.contracts.common import IntegerScalar
from structuraguard.exceptions import LoadError
from structuraguard.loading.groups import dependency_groups
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


async def test_dependency_closure_preserves_order_and_covers_every_unit_once() -> None:
    _, _, catalog, _, _ = await case()
    snapshot, policy = await dry_run_case(
        catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=i)}
            for i in (2, 3, 4)
        ],
        table_name="orders",
    )
    prepared = await prepare(snapshot)
    plan = await build_plan(prepared, catalog=catalog, policy=policy, reader=Keys())
    a, b, c = plan.steps
    plan = plan.model_copy(
        update={
            "steps": (
                a,
                b.model_copy(update={"dependencies": (a.unit_id,)}),
                c,
            )
        }
    )
    assert dependency_groups(prepared, plan, 100) == (
        (a.unit_id, b.unit_id),
        (c.unit_id,),
    )
    with pytest.raises(LoadError, match="SECURITY_LIMIT_EXCEEDED"):
        dependency_groups(prepared, plan, 1)
