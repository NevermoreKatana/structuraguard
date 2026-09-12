"""M12 AC-05: недостающие null/missing/type/empty-scope случаи DSL."""

from datetime import date

import pytest
from tests.contract.validation.test_rule_operations import literal
from tests.unit.validation.test_business_rules import dataset, field, row, rules

from structuraguard.contracts.business_rules import BusinessRuleSet, RuleReference
from structuraguard.validation import BusinessRuleValidator


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,case",
    [
        (op, case)
        for op in ("not_equals", "gt", "gte", "lt", "lte", "date_lt", "date_lte")
        for case in ("null", "missing", "wrong_type")
        if (op, case) != ("gt", "wrong_type")
    ],
)
async def test_comparison_missing_null_and_types_have_exact_issues(
    op: str, case: str
) -> None:
    kind = "date" if op.startswith("date_") else "decimal"
    expected = {
        "null": "RULE_NULL_OPERAND",
        "missing": "RULE_MISSING_OPERAND",
        "wrong_type": "RULE_TYPE_MISMATCH",
    }[case]
    value = (
        {"kind": "null"}
        if case == "null"
        else {"kind": "string", "value": "2026-09-13"}
    )
    data = dataset(row("r", **({} if case == "missing" else {"left": value})))
    spec = BusinessRuleSet.model_validate(
        {
            "fields": (
                {"collection_id": "orders", "field_id": "left", "value_type": kind},
            ),
            "rules": (
                {
                    "op": op,
                    "rule_id": "compare",
                    "collection_id": "orders",
                    "left": field("left", kind),
                    "right": literal(
                        date(2026, 9, 13) if kind == "date" else "1", kind
                    ),
                },
            ),
        }
    )
    result = await BusinessRuleValidator().validate(data, rules=spec)
    assert [issue.code for issue in result.issues] == [expected]
    assert result.issues[0].record_id == "r"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case,code",
    [
        ("false", None),
        ("null", "RULE_NULL_OPERAND"),
        ("missing", "RULE_MISSING_OPERAND"),
        ("wrong_type", "RULE_TYPE_MISMATCH"),
    ],
)
async def test_required_if_does_not_invent_dependent_presence_error(
    case: str, code: str | None
) -> None:
    left = (
        {"kind": "null"}
        if case == "null"
        else {"kind": "string", "value": "1"}
        if case == "wrong_type"
        else {"kind": "decimal", "value": "0"}
    )
    data = dataset(row("r", **({} if case == "missing" else {"other": left})))
    spec = rules(
        {
            "op": "required_if",
            "rule_id": "conditional",
            "collection_id": "orders",
            "field_id": "total",
            "left": field("other"),
            "right": literal("1"),
        }
    )
    result = await BusinessRuleValidator().validate(data, rules=spec)
    assert [issue.code for issue in result.issues] == ([] if code is None else [code])
    assert result.accepted is (code is None)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,values,accepted",
    [
        ("at_least_one", {"total": {"kind": "null"}, "other": {"kind": "null"}}, False),
        (
            "mutually_exclusive",
            {"total": {"kind": "null"}, "other": {"kind": "null"}},
            True,
        ),
        (
            "mutually_exclusive",
            {"total": {"kind": "null"}, "other": {"kind": "decimal", "value": "0"}},
            True,
        ),
    ],
)
async def test_presence_counts_non_null_values_including_zero(
    op: str, values: dict[str, object], accepted: bool
) -> None:
    result = await BusinessRuleValidator().validate(
        dataset(row("r", **values)),
        rules=rules(
            {
                "op": op,
                "rule_id": "presence",
                "collection_id": "orders",
                "field_ids": ("total", "other"),
            }
        ),
    )
    assert result.accepted is accepted
    assert [issue.code for issue in result.issues] == (
        [] if accepted else ["RULE_VIOLATION"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "operand,case",
    [
        ("total", "null"),
        ("total", "missing"),
        ("price", "missing"),
        ("price", "wrong_type"),
    ],
)
async def test_sum_unavailable_operand_does_not_create_false_sum_violation(
    operand: str, case: str
) -> None:
    total: dict[str, object] = {"kind": "decimal", "value": "1"}
    price: dict[str, object] = {"kind": "decimal", "value": "1"}
    if case == "null":
        total = {"kind": "null"}
    elif case == "wrong_type":
        price = {"kind": "boolean", "value": True}
    parent = row(
        "p", **({} if operand == "total" and case == "missing" else {"total": total})
    )
    child = {
        **row(
            "c",
            "lines",
            **({} if operand == "price" and case == "missing" else {"price": price}),
        ),
        "parent_id": "p",
    }
    result = await BusinessRuleValidator().validate(
        dataset(parent, child),
        rules=rules(
            {
                "op": "sum_equals",
                "rule_id": "sum",
                "collection_id": "orders",
                "total": field("total"),
                "item_collection_id": "lines",
                "terms": (field("price"),),
            }
        ),
    )
    expected = {
        "null": "RULE_NULL_OPERAND",
        "missing": "RULE_MISSING_OPERAND",
        "wrong_type": "RULE_TYPE_MISMATCH",
    }[case]
    assert [issue.code for issue in result.issues] == [expected]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,child,count,accepted",
    [
        ("min_items", False, 0, True),
        ("max_items", False, 0, True),
        ("min_items", True, 1, True),
        ("max_items", True, 0, False),
    ],
)
async def test_item_count_empty_scope_and_null_valued_child(
    op: str, child: bool, count: int, accepted: bool
) -> None:
    rows = (
        (row("p"), {**row("c", "lines", price={"kind": "null"}), "parent_id": "p"})
        if child
        else (row("p"),)
    )
    result = await BusinessRuleValidator().validate(
        dataset(*rows),
        rules=rules(
            {
                "op": op,
                "rule_id": "count",
                "collection_id": "orders",
                "item_collection_id": "lines",
                "count": count,
            }
        ),
    )
    assert result.accepted is accepted
    assert [issue.code for issue in result.issues] == (
        [] if accepted else ["RULE_VIOLATION"]
    )


@pytest.mark.anyio
@pytest.mark.parametrize("op", ["min_items", "max_items"])
async def test_item_count_rejects_orphan_instead_of_hiding_it(op: str) -> None:
    result = await BusinessRuleValidator().validate(
        dataset(row("p"), row("c", "lines")),
        rules=rules(
            {
                "op": op,
                "rule_id": "count",
                "collection_id": "orders",
                "item_collection_id": "lines",
                "count": 0,
            }
        ),
    )
    assert [issue.code for issue in result.issues] == ["RULE_PARENT_SCOPE_INVALID"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "op,case",
    [
        ("unique_by", "missing"),
        ("unique_by", "wrong_type"),
        ("matches_reference", "missing"),
        ("matches_reference", "null"),
        ("matches_reference", "wrong_type"),
        ("matches_reference", "absent_key"),
    ],
)
async def test_keys_require_typed_present_operands(op: str, case: str) -> None:
    reference = RuleReference.model_validate(
        {
            "reference_id": "allowed",
            "value_types": ("decimal",),
            "keys": ({"values": ({"kind": "decimal", "value": "1"},)},),
        }
    )
    rule: dict[str, object] = {
        "op": op,
        "rule_id": "keys",
        "collection_id": "orders",
        "fields": (field("total"),),
    }
    if op == "unique_by":
        rule.update(scope="collection", nulls="reject")
    else:
        rule.update(
            reference_id=reference.reference_id,
            reference_fingerprint=reference.fingerprint,
        )
    values = (
        {}
        if case == "missing"
        else {"total": {"kind": "null"}}
        if case == "null"
        else {"total": {"kind": "boolean", "value": True}}
        if case == "wrong_type"
        else {"total": {"kind": "decimal", "value": "2"}}
    )
    result = await BusinessRuleValidator(references=(reference,)).validate(
        dataset(row("r", **values)), rules=rules(rule)
    )
    assert [issue.code for issue in result.issues] == [
        {
            "missing": "RULE_MISSING_OPERAND",
            "null": "RULE_NULL_OPERAND",
            "wrong_type": "RULE_TYPE_MISMATCH",
            "absent_key": "RULE_VIOLATION",
        }[case]
    ]


@pytest.mark.anyio
async def test_parent_scoped_uniqueness_keeps_identical_keys_of_other_parents() -> None:
    root1, root2 = row("p1"), row("p2")

    def child(identity: str, parent: str) -> dict[str, object]:
        return {
            **row(identity, "lines", price={"kind": "decimal", "value": "1"}),
            "parent_id": parent,
        }

    result = await BusinessRuleValidator().validate(
        dataset(root1, root2, child("a", "p1"), child("b", "p2"), child("c", "p1")),
        rules=rules(
            {
                "op": "unique_by",
                "rule_id": "key",
                "collection_id": "lines",
                "fields": (field("price"),),
                "scope": "parent",
                "nulls": "reject",
            }
        ),
    )
    assert [(issue.code, issue.record_id) for issue in result.issues] == [
        ("RULE_VIOLATION", "a"),
        ("RULE_VIOLATION", "c"),
    ]
