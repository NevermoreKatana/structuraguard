"""Legacy hashes и новые descriptors/evidence не меняют прежние DTO гарантии."""

import pytest
from pydantic import ValidationError
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.mapping import (
    MappingPlan,
    MappingPlanValidationResult,
    ValidatedMappingPlan,
)
from structuraguard.contracts.mapping_rules import MappingIdentity
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


async def test_legacy_wire_omits_m11_fields_and_preserves_hash() -> None:
    plan, *_ = await case()
    payload = plan.model_dump(mode="json")
    assert "identities" not in payload and "relations" not in payload
    assert MappingPlan.model_validate_json(plan.model_dump_json()) == plan
    identity = MappingIdentity(
        table_id="public.orders", kind="explicit", column_ids=("id",)
    )
    with pytest.raises(ValidationError):
        replan(plan, identities=(identity,))
    extended = replan(plan, schema_version="1.1.0", identities=(identity,))
    assert extended.fingerprint != plan.fingerprint
    assert MappingPlan.model_validate_json(extended.model_dump_json()) == extended


async def test_result_and_wrapper_must_share_m11_evidence() -> None:
    plan, manifest, db, policy, profile = await case()
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert (
        MappingPlanValidationResult.model_validate_json(result.model_dump_json())
        == result
    )
    assert result.validated_plan is not None
    payload = result.validated_plan.model_dump(mode="python")
    payload.pop("evidence")
    legacy = ValidatedMappingPlan.model_validate(payload)
    with pytest.raises(ValidationError):
        MappingPlanValidationResult.model_validate(
            result.model_copy(update={"validated_plan": legacy}).model_dump(
                mode="python"
            )
        )
