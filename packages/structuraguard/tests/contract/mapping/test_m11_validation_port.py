"""Одинаковые decisions для typed/JSON входов реализации port."""

import pytest
from tests.fakes.mapping_validation import case

from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.mapping import MappingPlanValidator
from structuraguard.ports.mapping import MappingPlanValidator as ValidatorPort

pytestmark = pytest.mark.anyio


async def test_validator_port_and_json_produce_same_evidence() -> None:
    plan, manifest, db, policy, profile = await case()
    validator = MappingPlanValidator(policy=policy)
    assert isinstance(validator, ValidatorPort)
    typed = await validator.validate(plan, manifest, db, profile=profile)
    wire = await validator.validate_json(
        plan.model_dump_json().encode(), manifest, db, profile=profile
    )
    assert isinstance(typed, MappingPlanValidationResult)
    assert isinstance(wire, MappingPlanValidationResult)
    assert typed.validation_fingerprint == wire.validation_fingerprint
    assert typed.evidence == wire.evidence
