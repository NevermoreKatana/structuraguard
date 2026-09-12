"""Решение M11 по настоящему M8 evidence: представление, потери и недоказанные типы."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import (
    BooleanScalar,
    DateTimeScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    StringScalar,
    ValidationDecision,
)
from structuraguard.contracts.database import DatabaseType
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("amount", "target", "code"),
    [
        pytest.param(
            BooleanScalar(value=True),
            DatabaseType(native_type="integer", canonical_type="integer"),
            "MAPPING_TYPE_INCOMPATIBLE",
            id="bool-is-not-int",
        ),
        pytest.param(
            DecimalScalar(value=Decimal("0.1")),
            DatabaseType(native_type="double precision", canonical_type="float"),
            "MAPPING_TYPE_INCOMPATIBLE",
            id="decimal-to-float",
        ),
        pytest.param(
            StringScalar(value="123"),
            DatabaseType(native_type="integer", canonical_type="integer"),
            "MAPPING_TRANSFORMATION_REQUIRED",
            id="text-to-int",
        ),
        pytest.param(
            IntegerScalar(value=32768),
            DatabaseType(native_type="smallint", canonical_type="integer"),
            "MAPPING_NUMERIC_OVERFLOW",
            id="smallint-overflow",
        ),
        pytest.param(
            IntegerScalar(value=32767),
            DatabaseType(native_type="smallint", canonical_type="integer"),
            None,
            id="smallint-boundary",
        ),
        pytest.param(
            StringScalar(value="abcd"),
            DatabaseType(native_type="varchar", canonical_type="text", length=3),
            "MAPPING_LENGTH_OVERFLOW",
            id="length-overflow",
        ),
        pytest.param(
            StringScalar(value="abc"),
            DatabaseType(native_type="varchar", canonical_type="text", length=3),
            None,
            id="length-boundary",
        ),
        pytest.param(
            DecimalScalar(value=Decimal("100")),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=2
            ),
            "MAPPING_NUMERIC_OVERFLOW",
            id="precision-overflow",
        ),
        pytest.param(
            DecimalScalar(value=Decimal("1.234")),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=2
            ),
            "MAPPING_TRANSFORMATION_REQUIRED",
            id="scale-rounding",
        ),
        pytest.param(
            DecimalScalar(value=Decimal("1.2300")),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=2
            ),
            None,
            id="scale-trailing-zeroes",
        ),
        pytest.param(
            DecimalScalar(value=Decimal("0.0000")),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=2
            ),
            None,
            id="scale-zero",
        ),
        pytest.param(
            IntegerScalar(value=12),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=-1
            ),
            "MAPPING_TRANSFORMATION_REQUIRED",
            id="negative-scale-rounding",
        ),
        pytest.param(
            IntegerScalar(value=10),
            DatabaseType(
                native_type="numeric", canonical_type="decimal", precision=4, scale=-1
            ),
            None,
            id="negative-scale-exact",
        ),
        pytest.param(
            DateTimeScalar(value=datetime(2026, 1, 1, tzinfo=UTC)),
            DatabaseType(
                native_type="timestamp", canonical_type="datetime", timezone=False
            ),
            "MAPPING_TRANSFORMATION_REQUIRED",
            id="timezone-loss",
        ),
        pytest.param(
            DateTimeScalar(value=datetime(2026, 1, 1, tzinfo=UTC)),
            DatabaseType(
                native_type="timestamptz", canonical_type="datetime", timezone=True
            ),
            None,
            id="timezone-preserved",
        ),
        pytest.param(
            IntegerScalar(value=12),
            DatabaseType(
                native_type="mystery", canonical_type="unknown", type_kind="unknown"
            ),
            "MAPPING_TYPE_UNVERIFIED",
            id="unknown",
        ),
        pytest.param(
            StringScalar(value="new"),
            DatabaseType(
                native_type="status",
                canonical_type="text",
                type_kind="enum",
                enum_labels=("new", "done"),
            ),
            None,
            id="enum-representation",
        ),
        pytest.param(
            StringScalar(value="[1]"),
            DatabaseType(
                native_type="integer[]",
                canonical_type="unknown",
                type_kind="array",
                element_type=DatabaseType(
                    native_type="integer", canonical_type="integer"
                ),
            ),
            "MAPPING_TYPE_UNVERIFIED",
            id="array-unproven",
        ),
        pytest.param(
            IntegerScalar(value=12),
            DatabaseType(
                native_type="positive",
                canonical_type="integer",
                type_kind="domain",
                base_type=DatabaseType(native_type="integer", canonical_type="integer"),
                domain_checks=("VALUE > 0",),
            ),
            None,
            id="domain-base",
        ),
    ],
)
async def test_type_evidence_controls_decision_and_wrapper(
    amount: NormalizedScalar, target: DatabaseType, code: str | None
) -> None:
    plan, manifest, db, policy, profile = await case(
        amount=amount, semantic_type="unresolved"
    )
    original = db.schemas[0].tables[0]
    pk, value = original.columns
    assert value.inspection is not None
    value = value.model_copy(
        update={
            "type_name": target.canonical_type,
            "inspection": value.inspection.model_copy(update={"data_type": target}),
        }
    )
    db = catalog(original.model_copy(update={"columns": (pk, value)}))
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    if code is None:
        assert result.decision is ValidationDecision.ACCEPTED
        assert result.validated_plan is not None
    else:
        assert code in {i.code for i in result.issues}
        assert result.decision is (
            ValidationDecision.NEEDS_REVIEW
            if code == "MAPPING_TYPE_UNVERIFIED"
            else ValidationDecision.REJECTED
        )
        assert result.validated_plan is None


async def test_missing_representation_evidence_needs_review_even_at_full_confidence() -> (
    None
):
    plan, manifest, db, policy, _ = await case(semantic_type="unresolved")
    result = await MappingPlanValidator(policy=policy).validate(plan, manifest, db)
    assert isinstance(result, MappingPlanValidationResult)
    assert result.decision is ValidationDecision.NEEDS_REVIEW
    assert {i.code for i in result.issues} == {"MAPPING_TYPE_UNVERIFIED"}
    assert result.validated_plan is None
