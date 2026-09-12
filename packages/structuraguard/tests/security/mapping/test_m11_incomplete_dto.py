"""Неполные DTO не обходят безопасный error contract preflight."""

import pytest
from tests.fakes.mapping_validation import case

from structuraguard.contracts import (
    ColumnCatalog,
    DatabaseCatalog,
    MappingPlan,
    MappingValidationPolicy,
    NormalizedDataProfile,
    NormalizedDatasetManifest,
)
from structuraguard.exceptions import ValidationError
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "boundary", ["plan", "manifest", "catalog", "profile", "policy", "column"]
)
async def test_missing_dto_fields_raise_safe_sdk_error(boundary: str) -> None:
    plan, manifest, db, policy, profile = await case()
    if boundary == "plan":
        plan = MappingPlan.model_construct()
    elif boundary == "manifest":
        manifest = NormalizedDatasetManifest.model_construct()
    elif boundary == "catalog":
        db = DatabaseCatalog.model_construct()
    elif boundary == "profile":
        profile = NormalizedDataProfile.model_construct()
    elif boundary == "policy":
        policy = MappingValidationPolicy.model_construct()
    else:
        schema = db.schemas[0]
        table = schema.tables[0].model_copy(
            update={"columns": (ColumnCatalog.model_construct(),)}
        )
        db = db.model_copy(
            update={"schemas": (schema.model_copy(update={"tables": (table,)}),)}
        )
    with pytest.raises(ValidationError) as caught:
        await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
    assert caught.value.error_code == "MAPPING_PLAN_INVALID"
    assert "AttributeError" not in str(caught.value)
