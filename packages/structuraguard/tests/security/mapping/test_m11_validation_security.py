"""SQL identifiers/DDL и повреждённые snapshots не дают checked wrapper."""

import asyncio
import json

import pytest
from tests.fakes.mapping import catalog
from tests.fakes.mapping_validation import bind_catalog, case, replan

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.database import (
    CatalogColumnRef,
    IndexCatalog,
    IndexKeyCatalog,
)
from structuraguard.contracts.mapping_validation import (
    MappingPlanInputReport,
    MappingValidationOptions,
)
from structuraguard.exceptions import ValidationError
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "identifier",
    [
        "orders; DROP TABLE orders; --",
        "orders/*comment*/",
        "pg_catalog.pg_authid",
        "orders' OR 1=1--",
        "public.оrders",
    ],
)
async def test_unlisted_malicious_identifiers_remain_inert(identifier: str) -> None:
    plan, manifest, db, policy, profile = await case()
    first, second = plan.mappings
    plan = replan(
        plan,
        mappings=(
            first,
            second.model_copy(
                update={
                    "target": CatalogColumnRef(table_id=identifier, column_id="amount")
                }
            ),
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert result.decision is ValidationDecision.REJECTED
    assert "MAPPING_TABLE_NOT_FOUND" in {i.code for i in result.issues}
    assert identifier not in str(result.issues)


async def test_sql_forbidden_operation_and_unrelated_semantic_issues_are_collected() -> (
    None
):
    plan, manifest, db, policy, profile = await case()
    payload = plan.model_dump(mode="json")
    payload.update(
        sql="DROP TABLE secrets; -- payload-canary",
        operation="DROP TABLE",
        confidence="0.1",
    )
    payload.pop("fingerprint")
    result = await MappingPlanValidator(policy=policy).validate_json(
        json.dumps(payload).encode(), manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanInputReport)
    assert {i.code for i in result.issues} >= {
        "MAPPING_SQL_FORBIDDEN",
        "MAPPING_OPERATION_FORBIDDEN",
        "MAPPING_DDL_FORBIDDEN",
        "MAPPING_CONFIDENCE_BELOW_THRESHOLD",
    }
    assert "payload-canary" not in result.canonical_json()


async def test_stale_plan_model_copy_is_revalidated() -> None:
    plan, manifest, db, policy, profile = await case()
    result = await MappingPlanValidator(policy=policy).validate(
        plan.model_copy(update={"revision": 2}), manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanInputReport)
    assert "MAPPING_PLAN_FINGERPRINT_MISMATCH" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    "payload", [b'{"x":1,"x":2}', b"[]", b"{", b'{"operation":null}']
)
async def test_invalid_json_is_safe_rejection(payload: bytes) -> None:
    _, manifest, db, policy, profile = await case()
    result = await MappingPlanValidator(policy=policy).validate_json(
        payload, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanInputReport)
    assert result.decision is ValidationDecision.REJECTED


async def test_resource_limit_does_not_return_partial_acceptance() -> None:
    plan, manifest, db, policy, profile = await case()
    validator = MappingPlanValidator(
        policy=policy, options=MappingValidationOptions(max_mappings=1)
    )
    with pytest.raises(ValidationError) as caught:
        await validator.validate(plan, manifest, db, profile=profile)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"
    with pytest.raises(ValidationError):
        await validator.validate_json(b"[" * 65, manifest, db)


@pytest.mark.parametrize("payload", [b'{"confidence":NaN}', b'{"x":"\\ud800"}'])
async def test_nonfinite_or_invalid_unicode_json_has_safe_failure(
    payload: bytes,
) -> None:
    _, manifest, db, policy, _ = await case()
    result = await MappingPlanValidator(policy=policy).validate_json(
        payload, manifest, db
    )
    assert isinstance(result, MappingPlanInputReport)
    assert not result.complete


async def test_nonfinite_model_copy_and_cancellation_do_not_produce_evidence() -> None:
    plan, manifest, db, policy, profile = await case()
    validator = MappingPlanValidator(policy=policy)
    with pytest.raises(ValidationError):
        await validator.validate(
            plan.model_copy(update={"confidence": float("inf")}), manifest, db
        )
    task = asyncio.create_task(validator.validate(plan, manifest, db, profile=profile))
    asyncio.get_running_loop().call_soon(task.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    ("schema", "name", "accepted"),
    [
        ("pg_catalog", "pg_authid", False),
        ("public", "orders'; DROP TABLE orders; --", True),
    ],
)
async def test_system_metadata_is_denied_but_exact_quoted_identifiers_are_inert(
    schema: str, name: str, accepted: bool
) -> None:
    plan, manifest, db, _, profile = await case()
    target = (
        db.schemas[0]
        .tables[0]
        .model_copy(update={"schema_name": schema, "name": name, "table_id": name})
    )
    db = catalog(target)
    plan, policy = bind_catalog(plan, db, target)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert (result.decision is ValidationDecision.ACCEPTED) is accepted
    if not accepted:
        assert "MAPPING_SYSTEM_OBJECT_FORBIDDEN" in {i.code for i in result.issues}


async def test_constraint_work_is_charged_even_for_repeated_equivalent_keys() -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    assert target.inspection is not None
    indexes = tuple(
        IndexCatalog(
            index_id=f"index_{i}",
            name=f"index_{i}",
            keys=(IndexKeyCatalog(column_id="amount"),),
            unique=True,
            origin="index",
        )
        for i in range(200)
    )
    db = catalog(
        target.model_copy(
            update={
                "inspection": target.inspection.model_copy(update={"indexes": indexes})
            }
        )
    )
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    validator = MappingPlanValidator(
        policy=policy, options=MappingValidationOptions(max_operations=100)
    )
    with pytest.raises(ValidationError) as caught:
        await validator.validate(plan, manifest, db, profile=profile)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"
