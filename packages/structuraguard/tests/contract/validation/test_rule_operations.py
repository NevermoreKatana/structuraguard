"""Каждая allowlisted операция проверяется через публичный evaluator."""

from datetime import date
from decimal import Decimal

import pytest
from tests.unit.validation.test_business_rules import dataset, field, row, rules

from structuraguard.contracts.business_rules import BusinessRuleSet, RuleReference
from structuraguard.contracts.common import DecimalScalar
from structuraguard.validation import BusinessRuleValidator


def literal(value: object, kind: str = "decimal") -> dict[str, object]:
    return {"kind": "literal", "value": {"kind": kind, "value": value}}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("op", "left", "right", "accepted"),
    [
        ("equals", 1, 1, True),
        ("equals", 1, 2, False),
        ("not_equals", 1, 2, True),
        ("not_equals", 1, 1, False),
        ("gt", 2, 1, True),
        ("gt", 1, 1, False),
        ("gte", 1, 1, True),
        ("gte", 0, 1, False),
        ("lt", 1, 2, True),
        ("lt", 2, 2, False),
        ("lte", 2, 2, True),
        ("lte", 3, 2, False),
    ],
)
async def test_comparisons(op: str, left: int, right: int, accepted: bool) -> None:
    spec = rules(
        {
            "op": op,
            "rule_id": "compare",
            "collection_id": "orders",
            "left": literal(str(left)),
            "right": literal(str(right)),
        }
    )
    result = await BusinessRuleValidator().validate(dataset(row("p")), rules=spec)
    assert result.accepted is accepted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("op", "a", "b", "accepted"),
    [
        ("date_lt", "2024-02-29", "2024-03-01", True),
        ("date_lt", "2024-02-29", "2024-02-29", False),
        ("date_lte", "2024-02-29", "2024-02-29", True),
        ("date_lte", "2025-01-01", "2024-12-31", False),
    ],
)
async def test_dates(op: str, a: str, b: str, accepted: bool) -> None:
    spec = rules(
        {
            "op": op,
            "rule_id": "dates",
            "collection_id": "orders",
            "left": literal(date.fromisoformat(a), "date"),
            "right": literal(date.fromisoformat(b), "date"),
        }
    )
    assert (
        await BusinessRuleValidator().validate(dataset(row("p")), rules=spec)
    ).accepted is accepted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("op", "values", "accepted"),
    [
        ("required_if", {}, False),
        ("required_if", {"total": {"kind": "null"}}, False),
        ("required_if", {"total": {"kind": "decimal", "value": "0"}}, True),
        ("at_least_one", {}, False),
        ("at_least_one", {"total": {"kind": "decimal", "value": "0"}}, True),
        ("mutually_exclusive", {}, True),
        (
            "mutually_exclusive",
            {
                "total": {"kind": "decimal", "value": "1"},
                "other": {"kind": "decimal", "value": "2"},
            },
            False,
        ),
    ],
)
async def test_presence(op: str, values: dict[str, object], accepted: bool) -> None:
    rule: dict[str, object] = {
        "op": op,
        "rule_id": "presence",
        "collection_id": "orders",
    }
    if op == "required_if":
        rule.update(
            field_id="total",
            left=literal(True, "boolean"),
            right=literal(True, "boolean"),
        )
    else:
        rule["field_ids"] = ("total", "other")
    assert (
        await BusinessRuleValidator().validate(
            dataset(row("p", **values)), rules=rules(rule)
        )
    ).accepted is accepted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("op", "count", "accepted"),
    [
        ("min_items", 1, True),
        ("min_items", 2, False),
        ("max_items", 1, True),
        ("max_items", 0, False),
    ],
)
async def test_item_count(op: str, count: int, accepted: bool) -> None:
    spec = rules(
        {
            "op": op,
            "rule_id": "count",
            "collection_id": "orders",
            "item_collection_id": "lines",
            "count": count,
        }
    )
    assert (
        await BusinessRuleValidator().validate(
            dataset(row("p"), {**row("c", "lines"), "parent_id": "p"}), rules=spec
        )
    ).accepted is accepted


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("nulls", "accepted", "code"),
    [
        ("distinct", True, None),
        ("equal", False, "RULE_VIOLATION"),
        ("reject", False, "RULE_NULL_OPERAND"),
    ],
)
async def test_unique_nulls(nulls: str, accepted: bool, code: str | None) -> None:
    spec = rules(
        {
            "op": "unique_by",
            "rule_id": "key",
            "collection_id": "orders",
            "fields": (field("total"), field("other")),
            "scope": "collection",
            "nulls": nulls,
        }
    )
    data = dataset(
        *(
            row(str(i), total={"kind": "null"}, other={"kind": "decimal", "value": "1"})
            for i in range(2)
        )
    )
    report = await BusinessRuleValidator().validate(data, rules=spec)
    assert report.accepted is accepted
    assert {i.code for i in report.issues} == ({code} if code else set())


@pytest.mark.anyio
async def test_reference_is_typed_and_fingerprint_bound() -> None:
    ref = RuleReference.model_validate(
        {
            "reference_id": "rates",
            "value_types": ("decimal",),
            "keys": ({"values": ({"kind": "decimal", "value": "2"},)},),
        }
    )
    rule: dict[str, object] = {
        "op": "matches_reference",
        "rule_id": "reference",
        "collection_id": "orders",
        "fields": (field("total"),),
        "reference_id": "rates",
        "reference_fingerprint": ref.fingerprint,
    }
    validator = BusinessRuleValidator(references=(ref,))
    data = dataset(row("p", total={"kind": "decimal", "value": "2"}))
    assert (await validator.validate(data, rules=rules(rule))).accepted
    rule["reference_fingerprint"] = "sha256:" + "0" * 64
    assert [
        i.code for i in (await validator.validate(data, rules=rules(rule))).issues
    ] == ["RULE_REFERENCE_INVALID"]


@pytest.mark.anyio
async def test_tolerance_requires_trusted_policy() -> None:
    spec = rules(
        {
            "op": "sum_equals",
            "rule_id": "sum",
            "collection_id": "orders",
            "total": field("total"),
            "item_collection_id": "lines",
            "terms": (field("price"),),
            "tolerance": {"kind": "decimal", "value": "0.01"},
        }
    )
    data = dataset(row("p", total={"kind": "decimal", "value": "0.01"}))
    assert not (await BusinessRuleValidator().validate(data, rules=spec)).accepted
    assert (
        await BusinessRuleValidator(
            max_tolerance=DecimalScalar(value=Decimal("0.01"))
        ).validate(data, rules=spec)
    ).accepted


@pytest.mark.parametrize("model", [BusinessRuleSet, RuleReference])
def test_schema_is_draft_json_compatible(
    model: type[BusinessRuleSet] | type[RuleReference],
) -> None:
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(model.model_json_schema())
