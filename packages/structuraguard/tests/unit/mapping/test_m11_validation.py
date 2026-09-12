"""Приёмка и полный отчёт независимых ошибок декларативного плана."""

from decimal import Decimal

import pytest
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.database import CatalogColumnRef
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.mapping.validation import MappingPlanValidator

pytestmark = pytest.mark.anyio


async def test_accepts_valid_plan_without_mutation() -> None:
    plan, manifest, db, policy, profile = await case()
    before = (plan.canonical_json(), db.canonical_json())
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is ValidationDecision.ACCEPTED
    assert result.validated_plan is not None
    assert result.evidence is not None
    assert result.evidence.load_order == ("public.orders",)
    assert (plan.canonical_json(), db.canonical_json()) == before


async def test_collects_independent_scope_reference_and_confidence_issues() -> None:
    plan, manifest, db, policy, profile = await case()
    first, second = plan.mappings
    changed = replan(
        plan,
        confidence=Decimal("0.1"),
        mappings=(
            first,
            second.model_copy(
                update={
                    "target": CatalogColumnRef(table_id="missing", column_id="amount")
                }
            ),
        ),
    )
    policy = policy.model_copy(update={"deny_tables": (("public", "orders"),)})
    result = await MappingPlanValidator(policy=policy).validate(
        changed, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert {i.code for i in result.issues} >= {
        "MAPPING_TABLE_DENIED",
        "MAPPING_TABLE_NOT_FOUND",
        "MAPPING_CONFIDENCE_BELOW_THRESHOLD",
    }
    assert result.validated_plan is None


async def test_detects_schema_drift_and_stale_catalog_claim() -> None:
    plan, manifest, db, policy, profile = await case()
    changed = replan(plan, database_fingerprint="sha256:" + "f" * 64)
    result = await MappingPlanValidator(policy=policy).validate(
        changed, manifest, db, profile=profile
    )
    assert "DATABASE_SCHEMA_DRIFT" in {i.code for i in result.issues}
    forged = db.model_copy(
        update={"database_fingerprint": changed.database_fingerprint}
    )
    result = await MappingPlanValidator(policy=policy).validate(
        changed, manifest, forged, profile=profile
    )
    assert {i.code for i in result.issues} >= {
        "DATABASE_SCHEMA_DRIFT",
        "MAPPING_CATALOG_FINGERPRINT_MISMATCH",
    }


async def test_fingerprint_is_independent_of_validation_time() -> None:
    plan, manifest, db, policy, profile = await case()
    validator = MappingPlanValidator(policy=policy)
    first = await validator.validate(plan, manifest, db, profile=profile)
    second = await validator.validate(plan, manifest, db, profile=profile)
    assert isinstance(first, MappingPlanValidationResult)
    assert isinstance(second, MappingPlanValidationResult)
    assert first.validation_fingerprint == second.validation_fingerprint
    assert first.issues == second.issues
