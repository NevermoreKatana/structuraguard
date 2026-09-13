"""Детерминированные решения плана и отделение quarantine от global blockers."""

import pytest
from pydantic import ValidationError
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case

from structuraguard.contracts.common import IntegerScalar, LoadOperation
from structuraguard.contracts.constraint_validation import (
    ConstraintMatch,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.loading import DryRunExecutionPlan
from structuraguard.database._constraint_queries import result
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


async def test_execution_plan_wire_round_trip_rejects_unknown_version() -> None:
    _, _, catalog, _, _ = await case()
    request, policy = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=12)}],
        table_name="orders",
    )
    plan = await build_plan(
        await prepare(request), catalog=catalog, policy=policy, reader=Keys()
    )
    restored = DryRunExecutionPlan.model_validate_json(plan.canonical_json())
    assert restored == plan and restored.fingerprint == plan.fingerprint
    payload = plan.model_dump(mode="python")
    payload["schema_version"] = "2.0.0"
    with pytest.raises(ValidationError):
        DryRunExecutionPlan.model_validate(payload)


class Keys:
    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        return result(
            request,
            tuple(
                ConstraintMatch(
                    lookup_id=k.lookup_id,
                    exists=k.values[0].value == 1,
                    conflicts=k.values[0].value == 1 and not k.identity_column_ids,
                )
                for k in request.lookups
            ),
        )


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_counts_bind_data_mapping_and_projection(
    operation: LoadOperation,
) -> None:
    _, _, catalog, _, _ = await case()
    request, policy = await dry_run_case(
        catalog,
        [
            {"id": IntegerScalar(value=1), "amount": IntegerScalar(value=12)},
            {"id": IntegerScalar(value=2), "amount": IntegerScalar(value=13)},
        ],
        table_name="orders",
        operation=operation,
        error_policy="quarantine_invalid",
    )
    prepared = await prepare(request)
    plan = await build_plan(prepared, catalog=catalog, policy=policy, reader=Keys())
    assert plan.ready
    assert plan.planned_inserts == 1
    assert plan.planned_updates == (1 if operation is LoadOperation.UPSERT else 0)
    assert plan.planned_quarantine == (0 if operation is LoadOperation.UPSERT else 1)
    assert plan.normalized_fingerprint == request.mapping.normalized_fingerprint
    assert plan.mapping_fingerprint == request.mapping.fingerprint
    assert (
        plan.fingerprint
        == (
            await build_plan(prepared, catalog=catalog, policy=policy, reader=Keys())
        ).fingerprint
    )
    assert "public.orders" not in repr(plan)
    assert "public.orders" not in str(plan.safe_summary())


async def test_atomic_conflict_blocks_entire_plan() -> None:
    _, _, catalog, _, _ = await case()
    request, policy = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=1), "amount": IntegerScalar(value=12)}],
        table_name="orders",
    )
    plan = await build_plan(
        await prepare(request), catalog=catalog, policy=policy, reader=Keys()
    )
    assert not plan.ready
    assert plan.blockers == ("DRY_RUN_ATOMIC_REJECTED",)
    assert plan.planned_quarantine == 1
