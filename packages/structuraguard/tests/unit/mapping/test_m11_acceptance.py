"""Недостающие наблюдаемые критерии приёмки M11: полнота и независимые bindings."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, rehash_profile
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.domain.database_fingerprint import database_fingerprint
from structuraguard.exceptions import ValidationError
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio
OTHER_HASH = "sha256:" + "e" * 64


async def test_five_independent_failures_keep_exact_codes_and_locations() -> None:
    plan, manifest, db, policy, _ = await case()
    target = db.schemas[0].tables[0]
    # PK действительно отсутствует; mismatch второго поля не зависит от source первого.
    db = catalog(
        target.model_copy(
            update={
                "primary_key": (),
                "columns": (
                    column("id", "integer"),
                    column("amount", "text", position=1),
                ),
            }
        )
    )
    first, second = plan.mappings
    plan = replan(
        plan,
        database_fingerprint=db.database_fingerprint,
        confidence=Decimal("0.1"),
        mappings=(
            first.model_copy(
                update={
                    "source": first.source.model_copy(update={"field_name": "absent"})
                }
            ),
            second,
        ),
    )
    policy = policy.model_copy(
        update={"scope": policy.scope.model_copy(update={"deny": (second.target,)})}
    )
    result = await MappingPlanValidator(policy=policy).validate(plan, manifest, db)
    assert isinstance(result, MappingPlanValidationResult)
    assert result.evidence is not None
    assert result.decision is ValidationDecision.REJECTED
    assert result.validated_plan is None
    assert [
        (issue.code, loc.section, loc.index, loc.component)
        for issue, loc in zip(result.issues, result.evidence.locations, strict=True)
    ] == [
        ("MAPPING_SOURCE_NOT_FOUND", "mappings", 0, 0),
        ("MAPPING_COLUMN_DENIED", "mappings", 1, 0),
        ("MAPPING_TYPE_INCOMPATIBLE", "mappings", 1, 0),
        ("MAPPING_IDENTITY_REQUIRED", "catalog", 0, 0),
        ("MAPPING_CONFIDENCE_BELOW_THRESHOLD", "plan", 0, 0),
    ]


async def test_missing_table_suppresses_dependent_type_and_key_issues() -> None:
    plan, manifest, db, policy, _ = await case()
    plan = replan(
        plan,
        mappings=tuple(
            m.model_copy(
                update={"target": m.target.model_copy(update={"table_id": "absent"})}
            )
            for m in plan.mappings
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(plan, manifest, db)
    assert isinstance(result, MappingPlanValidationResult)
    assert result.evidence is not None
    assert [i.code for i in result.issues] == ["MAPPING_TABLE_NOT_FOUND"] * 2
    assert [(loc.section, loc.index) for loc in result.evidence.locations] == [
        ("mappings", 0),
        ("mappings", 1),
    ]
    assert result.validated_plan is None


@pytest.mark.parametrize(
    ("field", "index"),
    [
        ("source_fingerprint", 0),
        ("extraction_fingerprint", 1),
        ("parse_plan_fingerprint", 2),
        ("normalized_fingerprint", 3),
    ],
)
async def test_each_plan_lineage_binding_is_checked(field: str, index: int) -> None:
    plan, manifest, db, policy, profile = await case()
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, **{field: OTHER_HASH}), manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.evidence is not None
    assert [i.code for i in result.issues] == ["MAPPING_SOURCE_LINEAGE_MISMATCH"]
    assert [(loc.section, loc.index) for loc in result.evidence.locations] == [
        ("manifest", index)
    ]
    assert result.validated_plan is None


@pytest.mark.parametrize(
    "binding", ["plan_target", "scope_target", "plan_policy", "scope_policy"]
)
async def test_target_and_inspection_policy_bindings_are_not_schema_hash(
    binding: str,
) -> None:
    plan, manifest, db, policy, profile = await case()
    field = "target_id" if binding.endswith("target") else "target_policy_fingerprint"
    value = "another-target" if binding.endswith("target") else OTHER_HASH
    if binding.startswith("plan"):
        plan = replan(plan, **{field: value})
    else:
        policy = policy.model_copy(
            update={"scope": policy.scope.model_copy(update={field: value})}
        )
    assert plan.database_fingerprint == database_fingerprint(db)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    expected = (
        "MAPPING_TARGET_MISMATCH"
        if binding.endswith("target")
        else "MAPPING_POLICY_MISMATCH"
    )
    assert [i.code for i in result.issues] == [expected]
    assert result.validated_plan is None


async def test_validation_policy_changes_evidence_even_when_both_policies_accept() -> (
    None
):
    plan, manifest, db, policy, profile = await case()
    results = [
        await MappingPlanValidator(policy=p).validate(
            plan, manifest, db, profile=profile
        )
        for p in (
            policy,
            policy.model_copy(update={"confidence_threshold": Decimal("0.95")}),
        )
    ]
    first, second = results
    assert isinstance(first, MappingPlanValidationResult)
    assert isinstance(second, MappingPlanValidationResult)
    assert first.decision is second.decision is ValidationDecision.ACCEPTED
    assert first.evidence is not None and second.evidence is not None
    assert (
        first.evidence.actual_database_fingerprint
        == second.evidence.actual_database_fingerprint
    )
    assert first.evidence.policy_fingerprint != second.evidence.policy_fingerprint
    assert first.validation_fingerprint != second.validation_fingerprint


async def test_rehashed_profile_with_wrong_lineage_cannot_supply_type_evidence() -> (
    None
):
    plan, manifest, db, policy, profile = await case()
    profile = rehash_profile(profile, normalized_manifest_fingerprint=OTHER_HASH)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.evidence is not None
    assert [i.code for i in result.issues] == ["MAPPING_SOURCE_LINEAGE_MISMATCH"]
    assert result.evidence.locations[0].section == "profile"
    assert result.evidence.profile_fingerprint is None
    assert result.validated_plan is None


@pytest.mark.parametrize("snapshot", ["manifest", "profile", "constructed_catalog"])
async def test_forged_snapshots_are_revalidated(snapshot: str) -> None:
    plan, manifest, db, policy, profile = await case()
    if snapshot == "manifest":
        manifest = manifest.model_copy(update={"normalized_fingerprint": OTHER_HASH})
    elif snapshot == "profile":
        profile = profile.model_copy(update={"profile_fingerprint": OTHER_HASH})
    else:
        db = DatabaseCatalog.model_construct(
            **{
                **{name: getattr(db, name) for name in DatabaseCatalog.model_fields},
                "dialect": None,
            }
        )
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
    assert caught.value.error_code == "MAPPING_PLAN_INVALID"


async def test_legacy_catalog_is_readable_but_cannot_authorize_validation() -> None:
    plan, manifest, db, policy, _ = await case()
    legacy = DatabaseCatalog(
        dialect="postgresql",
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        producer=db.producer,
    )
    assert DatabaseCatalog.model_validate_json(legacy.canonical_json()) == legacy
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(policy=policy).validate(plan, manifest, legacy)
    assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"


@pytest.mark.parametrize("kind", ["view", "materialized_view", "readonly"])
async def test_non_table_or_readonly_target_cannot_get_wrapper(kind: str) -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    assert target.inspection is not None
    changed = target.model_copy(
        update={
            "writable": False,
            "columns": tuple(
                c.model_copy(update={"writable": False}) for c in target.columns
            ),
            "inspection": target.inspection
            if kind == "readonly"
            else target.inspection.model_copy(update={"kind": kind}),
        }
    )
    db = catalog(changed)
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert {i.code for i in result.issues} == {
        "MAPPING_TABLE_NOT_WRITABLE",
        "MAPPING_COLUMN_NOT_WRITABLE",
    }
    assert result.validated_plan is None
