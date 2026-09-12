"""Закрытые операции, явные null и точные инварианты записей."""

from collections.abc import Mapping
from decimal import localcontext

import pytest

from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.record_validation import ValidationDataset
from structuraguard.validation import BusinessRuleValidator


def dataset(*values: dict[str, object]) -> ValidationDataset:
    return ValidationDataset.model_validate({"records": values})


def row(
    identity: str, collection: str = "orders", /, **values: object
) -> dict[str, object]:
    return {
        "record_id": identity,
        "collection_id": collection,
        "values": tuple({"field_id": k, "value": v} for k, v in values.items()),
    }


def field(name: str, kind: str = "decimal") -> dict[str, str]:
    return {"kind": "field", "field_id": name, "value_type": kind}


def rules(*items: Mapping[str, object]) -> BusinessRuleSet:
    return BusinessRuleSet.model_validate(
        {
            "rules": items,
            "fields": (
                {
                    "collection_id": "orders",
                    "field_id": "total",
                    "value_type": "decimal",
                },
                {
                    "collection_id": "orders",
                    "field_id": "other",
                    "value_type": "decimal",
                },
                {
                    "collection_id": "lines",
                    "field_id": "price",
                    "value_type": "decimal",
                },
                {
                    "collection_id": "lines",
                    "field_id": "quantity",
                    "value_type": "integer",
                },
            ),
        }
    )


@pytest.mark.anyio
async def test_all_errors_and_null_are_explicit() -> None:
    spec = rules(
        {
            "op": "gt",
            "rule_id": "positive",
            "collection_id": "orders",
            "left": field("total"),
            "right": {"kind": "literal", "value": {"kind": "decimal", "value": "0"}},
        },
        {
            "op": "equals",
            "rule_id": "same",
            "collection_id": "orders",
            "left": field("total"),
            "right": field("other"),
        },
    )
    source = dataset(
        row("a", total={"kind": "decimal", "value": "-1"}, other={"kind": "null"}),
        row("b", total={"kind": "string", "value": "__import__('os')"}),
    )
    before = source.canonical_json()
    result = await BusinessRuleValidator().validate(source, rules=spec)
    assert {i.code for i in result.issues} == {
        "RULE_VIOLATION",
        "RULE_NULL_OPERAND",
        "RULE_TYPE_MISMATCH",
        "RULE_MISSING_OPERAND",
    }
    assert source.canonical_json() == before
    assert "__import__" not in repr(result)


@pytest.mark.anyio
async def test_sum_uses_complete_parent_scope_and_exact_decimal() -> None:
    spec = rules(
        {
            "op": "sum_equals",
            "rule_id": "sum",
            "collection_id": "orders",
            "total": field("total"),
            "item_collection_id": "lines",
            "terms": (
                {
                    "kind": "product",
                    "factors": (field("price"), field("quantity", "integer")),
                },
            ),
        }
    )
    data = dataset(
        row("p", total={"kind": "decimal", "value": "123456789.02"}),
        {
            **row(
                "c",
                "lines",
                price={"kind": "decimal", "value": "61728394.51"},
                quantity={"kind": "integer", "value": 2},
            ),
            "parent_id": "p",
        },
    )
    with localcontext() as ctx:
        ctx.prec = 2
        assert (await BusinessRuleValidator().validate(data, rules=spec)).accepted
    broken = dataset(row("p", total={"kind": "decimal", "value": "1"}))
    assert [
        i.code
        for i in (await BusinessRuleValidator().validate(broken, rules=spec)).issues
    ] == ["RULE_VIOLATION"]


@pytest.mark.anyio
async def test_sum_null_item_is_not_skipped_and_zero_empty_sum_is_valid() -> None:
    spec = rules(
        {
            "op": "sum_equals",
            "rule_id": "s",
            "collection_id": "orders",
            "total": field("total"),
            "item_collection_id": "lines",
            "terms": (field("price"),),
        }
    )
    root = row("p", total={"kind": "decimal", "value": "0"})
    assert (await BusinessRuleValidator().validate(dataset(root), rules=spec)).accepted
    report = await BusinessRuleValidator().validate(
        dataset(root, {**row("c", "lines", price={"kind": "null"}), "parent_id": "p"}),
        rules=spec,
    )
    assert [i.code for i in report.issues] == ["RULE_NULL_OPERAND"]


@pytest.mark.anyio
async def test_aggregate_does_not_silently_drop_unbound_child() -> None:
    spec = rules(
        {
            "op": "sum_equals",
            "rule_id": "s",
            "collection_id": "orders",
            "total": field("total"),
            "item_collection_id": "lines",
            "terms": (field("price"),),
        }
    )
    report = await BusinessRuleValidator().validate(
        dataset(
            row("p", total={"kind": "decimal", "value": "0"}),
            row("c", "lines", price={"kind": "decimal", "value": "9"}),
        ),
        rules=spec,
    )
    assert [i.code for i in report.issues] == ["RULE_PARENT_SCOPE_INVALID"]


@pytest.mark.anyio
async def test_explicit_null_comparison_policy() -> None:
    base = {
        "op": "equals",
        "rule_id": "r",
        "collection_id": "orders",
        "left": field("total"),
        "right": field("other"),
    }
    data = dataset(row("p", total={"kind": "null"}, other={"kind": "null"}))
    assert not (
        await BusinessRuleValidator().validate(data, rules=rules(base))
    ).accepted
    assert (
        await BusinessRuleValidator().validate(
            data, rules=rules({**base, "nulls": "equal"})
        )
    ).accepted
    assert (
        await BusinessRuleValidator().validate(
            data, rules=rules({**base, "nulls": "pass"})
        )
    ).accepted
