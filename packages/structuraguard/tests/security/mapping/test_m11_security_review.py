"""Regression tests конкретных путей эксплуатации, найденных в diff M11."""

import json
import warnings
from decimal import InvalidOperation, localcontext

import pytest
from tests.fakes.mapping import catalog, column, table
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import DatabaseCatalog, ForeignKeyCatalog
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.mapping_rules import MappingIdentity
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.exceptions import ValidationError
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
@pytest.mark.parametrize("declaration", ["explicit", "inferred"])
async def test_alternative_unique_key_cannot_authorize_unapproved_source_pk(
    operation: LoadOperation, declaration: str
) -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    pk, amount = target.columns
    db = catalog(
        target.model_copy(
            update={
                "columns": (pk, amount.model_copy(update={"nullable": False})),
                "unique_constraints": (("amount",),),
            }
        )
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        operation=operation,
        database_fingerprint=db.database_fingerprint,
        identities=(
            MappingIdentity(
                table_id=target.table_id, kind="explicit", column_ids=("amount",)
            ),
        )
        if declaration == "explicit"
        else None,
    )
    allowed = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert allowed.decision is ValidationDecision.ACCEPTED
    policy = policy.model_copy(update={"source_identity_allow": ()})
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is ValidationDecision.REJECTED
    assert result.validated_plan is None
    assert "MAPPING_SOURCE_IDENTITY_FORBIDDEN" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    "boundary", ["plan", "catalog", "manifest", "profile", "policy", "options"]
)
async def test_invalid_typed_input_does_not_leak_payload_in_serialization_warning(
    boundary: str,
) -> None:
    plan, manifest, db, policy, profile = await case()
    secret = "restricted-plan-secret-canary"
    malformed = {"token": secret}
    options = MappingValidationOptions()
    if boundary == "plan":
        plan = plan.model_copy(update={"plan_id": malformed})
    elif boundary == "catalog":
        db = db.model_copy(update={"target_id": malformed})
    elif boundary == "manifest":
        manifest = manifest.model_copy(update={"source": malformed})
    elif boundary == "profile":
        profile = profile.model_copy(update={"producer": malformed})
    elif boundary == "policy":
        policy = policy.model_copy(update={"policy_id": malformed})
    else:
        options = options.model_copy(update={"max_mappings": malformed})
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        try:
            result = await MappingPlanValidator(
                policy=policy, options=options
            ).validate(plan, manifest, db, profile=profile)
        except ValidationError as error:
            assert error.error_code in {
                "MAPPING_PLAN_INVALID",
                "MAPPING_POLICY_MISMATCH",
            }
            assert secret not in str(error)
        else:
            assert boundary == "plan"
            assert result.decision is ValidationDecision.REJECTED
            assert secret not in str(result)
    assert secret not in "\n".join(str(w.message) for w in captured)


@pytest.mark.parametrize("section", ["identities", "relations"])
async def test_descriptor_budget_rejects_before_whole_plan_validation(
    section: str,
) -> None:
    plan, manifest, db, policy, _ = await case()
    payload = plan.model_dump(mode="json")
    payload.pop("fingerprint")
    payload["schema_version"] = "1.1.0"
    descriptor: dict[str, object]
    if section == "identities":
        descriptor = {
            "table_id": "public.orders",
            "kind": "explicit",
            "column_ids": ["id"],
        }
    else:
        descriptor = {
            "foreign_key_id": "missing",
            "child_table_id": "public.orders",
            "parent_table_id": "public.orders",
            "child_column_ids": ["id"],
            "parent_column_ids": ["id"],
            "child_sources": [],
            "strategy": "lookup",
        }
    payload[section] = [descriptor, descriptor]
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(
            policy=policy, options=MappingValidationOptions(max_edges=1)
        ).validate_json(json.dumps(payload).encode(), manifest, db)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"


@pytest.mark.parametrize("exponent", [b"9999999999999999999", b"-9999999999999999999"])
async def test_json_decimal_outside_runtime_range_has_controlled_rejection(
    exponent: bytes,
) -> None:
    _, manifest, db, policy, _ = await case()
    with localcontext() as context:
        context.traps[InvalidOperation] = False
        before = context.flags.copy()
        result = await MappingPlanValidator(policy=policy).validate_json(
            b'{"confidence":1e' + exponent + b"}", manifest, db
        )
        assert context.flags == before
    assert result.decision is ValidationDecision.REJECTED
    assert "MAPPING_PLAN_INVALID" in {i.code for i in result.issues}


@pytest.mark.parametrize("budget", ["max_tables", "max_columns", "max_edges"])
async def test_catalog_limits_run_before_snapshot_serialization(
    budget: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, manifest, db, policy, _ = await case()
    target = db.schemas[0].tables[0]
    if budget == "max_tables":
        db = catalog(target, table("other", column("value")))
    elif budget == "max_edges":
        db = catalog(
            target.model_copy(
                update={
                    "foreign_keys": tuple(
                        ForeignKeyCatalog(
                            foreign_key_id=name,
                            column_ids=("id",),
                            referenced_table_id=target.table_id,
                            referenced_column_ids=("id",),
                        )
                        for name in ("fk_1", "fk_2")
                    )
                }
            )
        )
    plan = replan(plan, database_fingerprint=db.database_fingerprint)

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("catalog serialized before shape limit")

    monkeypatch.setattr(DatabaseCatalog, "model_dump", forbidden)
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(
            policy=policy, options=MappingValidationOptions.model_validate({budget: 1})
        ).validate(plan, manifest, db)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"


async def test_malformed_json_report_also_obeys_result_budget() -> None:
    _, manifest, db, policy, _ = await case()
    validator = MappingPlanValidator(
        policy=policy, options=MappingValidationOptions(max_result_bytes=1)
    )
    with pytest.raises(ValidationError) as caught:
        await validator.validate_json(b"{", manifest, db)
    assert caught.value.error_code == "MAPPING_LIMIT_EXCEEDED"
