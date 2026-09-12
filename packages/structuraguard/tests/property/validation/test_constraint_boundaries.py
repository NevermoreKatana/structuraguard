"""Hypothesis: точные суммы, Unicode/composite keys и независимость порядка batches."""

import asyncio
from decimal import Decimal, localcontext

from hypothesis import given
from hypothesis import strategies as st
from tests.unit.validation.test_business_rules import dataset, field, row, rules

from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.record_validation import ValidationDataset
from structuraguard.validation import BusinessRuleValidator


@given(st.lists(st.integers(-10000000, 10000000), max_size=30))
def test_decimal_sum_round_trip_and_context_independence(cents: list[int]) -> None:
    total = Decimal(sum(cents)).scaleb(-2)
    records = [row("p", total={"kind": "decimal", "value": total})]
    for i, amount in enumerate(cents):
        records.append(
            {
                **row(
                    f"c{i}",
                    "lines",
                    price={"kind": "decimal", "value": Decimal(amount).scaleb(-2)},
                ),
                "parent_id": "p",
            }
        )
    spec = rules(
        {
            "op": "sum_equals",
            "rule_id": "sum",
            "collection_id": "orders",
            "total": field("total"),
            "item_collection_id": "lines",
            "terms": (field("price"),),
        }
    )
    source = ValidationDataset.model_validate_json(dataset(*records).canonical_json())
    with localcontext() as ctx:
        ctx.prec = 2
        report = asyncio.run(BusinessRuleValidator().validate(source, rules=spec))
    assert report.accepted
    assert asyncio.run(
        BusinessRuleValidator().validate(dataset(*reversed(records)), rules=spec)
    ).accepted


@given(st.text(max_size=80), st.text(max_size=80))
def test_unicode_composite_keys_do_not_concatenate_or_normalize(a: str, b: str) -> None:
    spec = BusinessRuleSet.model_validate(
        {
            "fields": tuple(
                {"collection_id": "orders", "field_id": f, "value_type": "string"}
                for f in ("a", "b")
            ),
            "rules": (
                {
                    "op": "unique_by",
                    "rule_id": "u",
                    "collection_id": "orders",
                    "fields": (field("a", "string"), field("b", "string")),
                    "scope": "collection",
                    "nulls": "equal",
                },
            ),
        }
    )
    source = dataset(
        row("1", a={"kind": "string", "value": a}, b={"kind": "string", "value": b}),
        row("2", a={"kind": "string", "value": b}, b={"kind": "string", "value": a}),
    )
    report = asyncio.run(BusinessRuleValidator().validate(source, rules=spec))
    assert report.accepted is (a != b)
    assert source == ValidationDataset.model_validate_json(source.canonical_json())
