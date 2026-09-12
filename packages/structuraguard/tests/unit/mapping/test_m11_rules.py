"""Негативные сценарии scope, types, identity и FK по настоящим catalog DTO."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import (
    CatalogColumnRef,
    DatabaseType,
    ForeignKeyCatalog,
)
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.mapping_rules import MappingIdentity, MappingRelation
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("allow_schemas", (), "MAPPING_SCHEMA_DENIED"),
        ("allow_schemas", ("absent",), "MAPPING_SCHEMA_NOT_FOUND"),
        ("deny_schemas", ("public",), "MAPPING_SCHEMA_DENIED"),
        ("allow_tables", (), "MAPPING_TABLE_DENIED"),
        ("deny_tables", (("public", "orders"),), "MAPPING_TABLE_DENIED"),
        ("source_identity_allow", (), "MAPPING_IDENTITY_REQUIRED"),
        ("allowed_operations", (), "MAPPING_OPERATION_FORBIDDEN"),
    ],
)
async def test_policy_veto(field: str, value: object, code: str) -> None:
    plan, manifest, db, policy, profile = await case()
    result = await MappingPlanValidator(
        policy=policy.model_copy(update={field: value})
    ).validate(plan, manifest, db, profile=profile)
    assert code in {i.code for i in result.issues}
    assert result.decision is ValidationDecision.REJECTED


async def test_column_deny_wins_over_allow() -> None:
    plan, manifest, db, policy, profile = await case()
    policy = policy.model_copy(
        update={
            "scope": policy.scope.model_copy(
                update={"deny": (plan.mappings[1].target,)}
            )
        }
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert "MAPPING_COLUMN_DENIED" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("text", "MAPPING_TYPE_INCOMPATIBLE"),
        ("generated", "MAPPING_GENERATED_COLUMN"),
        ("readonly", "MAPPING_COLUMN_NOT_WRITABLE"),
    ],
)
async def test_column_metadata_veto(kind: str, code: str) -> None:
    plan, manifest, db, policy, profile = await case()
    table = db.schemas[0].tables[0]
    pk, amount = table.columns
    assert amount.inspection is not None
    inspection = amount.inspection
    if kind == "text":
        amount = amount.model_copy(
            update={
                "type_name": "text",
                "inspection": amount.inspection.model_copy(
                    update={
                        "data_type": amount.inspection.data_type.model_copy(
                            update={"canonical_type": "text", "native_type": "text"}
                        ),
                    }
                ),
            }
        )
    elif kind == "generated":
        amount = amount.model_copy(
            update={
                "generated": True,
                "writable": False,
                "inspection": inspection.model_copy(
                    update={
                        "generation_expression": "1 + 1",
                        "generation_storage": "stored",
                    }
                ),
            }
        )
    else:
        amount = amount.model_copy(update={"writable": False})
    changed = catalog(table.model_copy(update={"columns": (pk, amount)}))
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, database_fingerprint=changed.database_fingerprint),
        manifest,
        changed,
        profile=profile,
    )
    assert code in {i.code for i in result.issues}
    assert result.decision is ValidationDecision.REJECTED
    if kind == "readonly":
        assert changed.database_fingerprint == db.database_fingerprint


@pytest.mark.parametrize(
    ("score", "accepted"), [("0.899999", False), ("0.90", True), ("0.900001", True)]
)
async def test_inclusive_threshold_on_each_field(score: str, accepted: bool) -> None:
    plan, manifest, db, policy, profile = await case()
    first, second = plan.mappings
    plan = replan(
        plan, mappings=(first, second.model_copy(update={"confidence": Decimal(score)}))
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert (result.decision is ValidationDecision.ACCEPTED) is accepted


async def test_upsert_requires_confirmed_key_and_does_not_fallback_from_explicit_key() -> (
    None
):
    plan, manifest, db, policy, profile = await case()
    plan = replan(
        plan,
        operation=LoadOperation.UPSERT,
        schema_version="1.1.0",
        identities=(
            MappingIdentity(
                table_id="public.orders", kind="explicit", column_ids=("amount",)
            ),
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert "MAPPING_UPSERT_KEY_INVALID" in {i.code for i in result.issues}
    valid = replan(
        plan,
        identities=(
            MappingIdentity(
                table_id="public.orders", kind="explicit", column_ids=("id",)
            ),
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        valid, manifest, db, profile=profile
    )
    assert result.decision is ValidationDecision.ACCEPTED


async def test_required_target_is_not_optional_by_mapping_flag() -> None:
    plan, manifest, db, policy, profile = await case()
    plan = replan(
        plan, mappings=(plan.mappings[1].model_copy(update={"required": False}),)
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert {i.code for i in result.issues} >= {
        "MAPPING_REQUIRED_TARGET_MISSING",
        "MAPPING_IDENTITY_REQUIRED",
    }


async def test_missing_domain_not_null_is_required_even_when_column_flag_nullable() -> (
    None
):
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    pk, amount = target.columns
    assert amount.inspection is not None
    domain = DatabaseType(
        native_type="positive",
        canonical_type="integer",
        type_kind="domain",
        base_type=amount.inspection.data_type,
        domain_not_null=True,
    )
    amount = amount.model_copy(
        update={
            "inspection": amount.inspection.model_copy(update={"data_type": domain})
        }
    )
    changed = catalog(target.model_copy(update={"columns": (pk, amount)}))
    plan = replan(
        plan,
        mappings=(plan.mappings[0],),
        database_fingerprint=changed.database_fingerprint,
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, changed, profile=profile
    )
    assert "MAPPING_REQUIRED_TARGET_MISSING" in {i.code for i in result.issues}


async def test_missing_column_and_source_are_independent() -> None:
    plan, manifest, db, policy, profile = await case()
    first, second = plan.mappings
    mapping = second.model_copy(
        update={
            "source": second.source.model_copy(update={"field_name": "missing"}),
            "target": CatalogColumnRef(table_id="public.orders", column_id="missing"),
        }
    )
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, mappings=(first, mapping)), manifest, db, profile=profile
    )
    assert {i.code for i in result.issues} >= {
        "MAPPING_SOURCE_NOT_FOUND",
        "MAPPING_COLUMN_NOT_FOUND",
    }


async def test_fk_and_cycle_require_explicit_supported_resolution() -> None:
    plan, manifest, db, policy, profile = await case()
    table = db.schemas[0].tables[0]
    fk = ForeignKeyCatalog(
        foreign_key_id="parent",
        column_ids=("amount",),
        referenced_table_id=table.table_id,
        referenced_column_ids=("id",),
    )
    db = catalog(table.model_copy(update={"foreign_keys": (fk,)}))
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert {i.code for i in result.issues} >= {
        "MAPPING_RELATION_UNRESOLVED",
        "CYCLIC_DEPENDENCY_REQUIRES_STRATEGY",
    }
    relation = MappingRelation(
        foreign_key_id="parent",
        child_table_id=table.table_id,
        parent_table_id=table.table_id,
        child_column_ids=("amount",),
        parent_column_ids=("id",),
        child_sources=(plan.mappings[1].source,),
        parent_sources=(plan.mappings[0].source,),
        strategy="mapped_parent",
    )
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, schema_version="1.1.0", relations=(relation,)),
        manifest,
        db,
        profile=profile,
    )
    assert "CYCLIC_DEPENDENCY_REQUIRES_STRATEGY" in {i.code for i in result.issues}
    assert isinstance(result, MappingPlanValidationResult)
    assert result.validated_plan is None
