"""M12 security: явные budgets и ограничение очереди на недоверенных DTO."""

import tracemalloc
from datetime import UTC, datetime
from typing import cast

import pytest

from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.record_validation import (
    RecordValidationLimits,
    ValidationDataset,
)
from structuraguard.exceptions import ValidationError
from structuraguard.validation import BusinessRuleValidator


@pytest.mark.parametrize("limits", [False, 0])
def test_business_rule_limits_reject_falsey_non_dto(limits: object) -> None:
    with pytest.raises(ValidationError) as error:
        BusinessRuleValidator(limits=cast(RecordValidationLimits, limits))
    assert error.value.error_code == "RULE_INPUT_INVALID"


def test_business_rule_limits_never_call_foreign_truthiness_hook() -> None:
    calls: list[str] = []

    class ForgedLimits(RecordValidationLimits):
        def __bool__(self) -> bool:
            calls.append("truthiness")
            return False

    with pytest.raises(ValidationError) as error:
        BusinessRuleValidator(limits=ForgedLimits(max_evaluations=1))
    assert error.value.error_code == "RULE_INPUT_INVALID"
    assert calls == []


@pytest.mark.anyio
async def test_alias_fanout_cannot_overallocate_the_pending_intake_queue() -> None:
    node: object = None
    for _ in range(16):
        node = (node,) * 1024
    data = ValidationDataset(records=()).model_copy(update={"records": node})
    validator = BusinessRuleValidator(limits=RecordValidationLimits(max_nodes=2048))
    rules = BusinessRuleSet(fields=(), rules=())
    # Input создаётся до измерения; проверяем только дополнительные allocations
    # intake. Порог с запасом отличает bounded queue от многократного fanout.
    tracemalloc.start()
    try:
        with pytest.raises(ValidationError) as error:
            await validator.validate(data, rules=rules)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert peak < 400_000, peak


@pytest.mark.anyio
@pytest.mark.parametrize("boundary", ["schema", "rules", "provenance"])
async def test_intake_never_hashes_or_compares_foreign_class_identity(
    boundary: str,
) -> None:
    from tests.fakes.provenance import provenance_fixture

    from structuraguard.validation import JsonSchemaValidator, ProvenanceValidator

    class ForeignMeta(type):
        def __hash__(cls) -> int:
            raise RuntimeError("foreign-class-hash")

        def __eq__(cls, other: object) -> bool:
            raise RuntimeError("foreign-class-equality")

    class Foreign(metaclass=ForeignMeta):
        pass

    value = Foreign()
    if boundary == "schema":
        with pytest.raises(ValidationError) as error:
            await JsonSchemaValidator().validate(value, schema=True)
        assert error.value.error_code == "JSON_SCHEMA_INPUT_INVALID"
    elif boundary == "rules":
        data = ValidationDataset(records=()).model_copy(update={"records": (value,)})
        with pytest.raises(ValidationError) as error:
            await BusinessRuleValidator().validate(
                data, rules=BusinessRuleSet(fields=(), rules=())
            )
        assert error.value.error_code == "RULE_INPUT_INVALID"
    else:
        fixture = await provenance_fixture()
        context = fixture.context.model_copy(update={"run_id": value})
        with pytest.raises(ValidationError) as error:
            await ProvenanceValidator().validate(
                fixture.normalized,
                source_batches=fixture.physical,
                plan=fixture.plan,
                context=context,
                generated_at=datetime(2026, 9, 13, tzinfo=UTC),
            )
        assert error.value.error_code == "PROVENANCE_INPUT_INVALID"
