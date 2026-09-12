"""Исполняемые строки, forged DTO и бюджеты отклоняются до вычислений."""

import pytest
from pydantic import ValidationError as ContractError

from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.record_validation import ValidationDataset
from structuraguard.exceptions import ValidationError
from structuraguard.validation import BusinessRuleValidator


@pytest.mark.parametrize(
    "op", ["eval", "exec", "__import__", "os.system", "sql", "python", "SELECT 1"]
)
def test_operation_allowlist(op: str) -> None:
    with pytest.raises(ContractError):
        BusinessRuleSet.model_validate(
            {
                "fields": (),
                "rules": (
                    {
                        "op": op,
                        "rule_id": "r",
                        "collection_id": "x",
                        "expression": "1 + 1",
                    },
                ),
            }
        )


@pytest.mark.anyio
async def test_forged_rule_cannot_reach_dispatch() -> None:
    forged = BusinessRuleSet.model_construct(fields=(), rules=(object(),))
    with pytest.raises(ValidationError) as error:
        await BusinessRuleValidator().validate(
            ValidationDataset(records=()), rules=forged
        )
    assert error.value.error_code == "RULE_INPUT_INVALID"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload", ["__class__", "__dict__[password]", "os.system('id')", "x; DROP TABLE y"]
)
async def test_field_names_are_exact_schema_ids(payload: str) -> None:
    from tests.unit.validation.test_business_rules import dataset, field, row, rules

    spec = rules(
        {
            "op": "equals",
            "rule_id": "r",
            "collection_id": "orders",
            "left": field(payload),
            "right": field("total"),
        }
    )
    report = await BusinessRuleValidator().validate(dataset(row("p")), rules=spec)
    assert [i.code for i in report.issues] == ["RULE_FIELD_UNKNOWN"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limit", "value"),
    [
        ("max_records", 1),
        ("max_evaluations", 1),
        ("max_issues", 1),
        ("max_keys", 1),
        ("max_bytes", 1),
        ("max_nodes", 1),
    ],
)
async def test_budgets_fail_without_partial_report(limit: str, value: int) -> None:
    from tests.unit.validation.test_business_rules import dataset, field, row, rules

    from structuraguard.contracts.record_validation import RecordValidationLimits

    spec = rules(
        {
            "op": "unique_by",
            "rule_id": "u",
            "collection_id": "orders",
            "fields": (field("total"),),
            "scope": "collection",
            "nulls": "equal",
        }
    )
    limits = RecordValidationLimits.model_validate({limit: value})
    with pytest.raises(ValidationError) as error:
        await BusinessRuleValidator(limits=limits).validate(
            dataset(
                *(
                    row(str(i), total={"kind": "decimal", "value": "1"})
                    for i in range(3)
                )
            ),
            rules=spec,
        )
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_input_cycles_and_subclasses_rejected_without_hooks() -> None:
    from structuraguard.contracts.record_validation import ValidationRecord

    class Hostile(ValidationRecord):
        def model_dump_json(self, **kwargs: object) -> str:
            raise AssertionError("untrusted serializer")

    forged = ValidationDataset.model_construct(
        records=(Hostile(record_id="p", collection_id="x", values=()),)
    )
    with pytest.raises(ValidationError):
        await BusinessRuleValidator().validate(
            forged, rules=BusinessRuleSet(fields=(), rules=())
        )


@pytest.mark.anyio
async def test_huge_decimal_exponent_rejected_before_fraction_expansion() -> None:
    from decimal import Decimal

    from tests.unit.validation.test_business_rules import dataset, row

    source = dataset(
        row("p", total={"kind": "decimal", "value": Decimal("1e999999999")})
    )
    with pytest.raises(ValidationError) as error:
        await BusinessRuleValidator().validate(
            source, rules=BusinessRuleSet(fields=(), rules=())
        )
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"
