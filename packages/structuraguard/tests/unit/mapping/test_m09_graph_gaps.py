"""Направление FK, цепочка с join payload и циклы проверяются по готовому M7 graph."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import (
    catalog,
    column,
    only_weight,
    profile,
    rehash_profile,
    scope_for,
    table,
)

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.database import ForeignKeyCatalog, TableCatalog
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import FieldRelationship
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("reverse", [False, True])
async def test_fk_chain_and_join_payload_use_only_direct_oriented_evidence(
    reverse: bool,
) -> None:
    entities = {
        "customer_key": "customer",
        "customer_ref": "order",
        "order_key": "order",
        "order_ref": "item",
        "product_key": "product",
        "product_ref": "item",
        "quantity": "item",
        "isolated_key": "isolated",
    }
    data = await profile({name: IntegerScalar(value=42) for name in entities})
    fields = []
    for source_field in data.fields:
        ref = SemanticFieldRef(
            entity_type=entities[source_field.field.field_name],
            field_name=source_field.field.field_name,
        )
        fields.append(
            source_field.model_copy(
                update={
                    "field": ref,
                    "pii": source_field.pii.model_copy(update={"field": ref}),
                }
            )
        )
    by_name = {f.field.field_name: f.field for f in fields}
    pairs = (
        ("customer_key", "customer_ref"),
        ("order_key", "order_ref"),
        ("product_key", "product_ref"),
    )
    data = rehash_profile(
        data,
        fields=tuple(fields),
        relationships=tuple(
            FieldRelationship(
                left=by_name[b if reverse else a],
                right=by_name[a if reverse else b],
                kind="parent_child",
                count=25,
            )
            for a, b in pairs
        ),
    )

    def fk(name: str, child: str, parent_table: str, parent: str) -> ForeignKeyCatalog:
        return ForeignKeyCatalog(
            foreign_key_id=name,
            column_ids=(child,),
            referenced_table_id="public." + parent_table,
            referenced_column_ids=(parent,),
        )

    db = catalog(
        table("customers", column("customer_key", "integer")),
        table(
            "orders",
            column("customer_ref", "integer"),
            column("order_key", "integer", position=1),
            foreign_keys=(
                fk("customer_fk", "customer_ref", "customers", "customer_key"),
            ),
        ),
        table("products", column("product_key", "integer")),
        table(
            "order_items",
            column("order_ref", "integer"),
            column("product_ref", "integer", position=1),
            column("quantity", "integer", position=2),
            foreign_keys=(
                fk("order_fk", "order_ref", "orders", "order_key"),
                fk("product_fk", "product_ref", "products", "product_key"),
            ),
        ),
        table("isolated", column("isolated_key", "integer")),
    )
    result = await DeterministicMapper().rank(data, db, scope=scope_for(db))
    expected = {
        "customer_ref": ("orders", "customer_fk"),
        "order_ref": ("order_items", "order_fk"),
        "product_ref": ("order_items", "product_fk"),
    }
    for name, (target, edge) in expected.items():
        field = result.field(entities[name], name)
        explanation = next(
            e
            for c, e in zip(field.candidates, field.explanations, strict=True)
            if c.target.table_id == "public." + target and c.target.column_id == name
        )
        signal = next(
            s for s in explanation.signals if s.code == "database_relation_score"
        )
        assert signal.value == (0 if reverse else 1)
        assert explanation.foreign_key_ids == (() if reverse else (edge,))
        assert "FK_UNRESOLVED" in explanation.blockers
    for name in ("quantity", "isolated_key"):
        field = result.field(entities[name], name)
        signal = next(
            s
            for s in field.explanations[0].signals
            if s.code == "database_relation_score"
        )
        assert signal.value == 0 and not signal.available
        assert not field.explanations[0].foreign_key_ids


@pytest.mark.parametrize("cycle", ["self", "scc"])
async def test_actual_cycle_target_requires_strategy_even_at_score_one(
    cycle: str,
) -> None:
    parent_id = "public.a" if cycle == "self" else "public.b"
    a = table(
        "a",
        column("account_key", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="a_fk",
                column_ids=("account_key",),
                referenced_table_id=parent_id,
                referenced_column_ids=(
                    "account_key" if cycle == "self" else "other_key",
                ),
            ),
        ),
    )
    tables: tuple[TableCatalog, ...] = (a,)
    if cycle == "scc":
        b = table(
            "b",
            column("other_key", "integer"),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="b_fk",
                    column_ids=("other_key",),
                    referenced_table_id=a.table_id,
                    referenced_column_ids=("account_key",),
                ),
            ),
        )
        tables += (b,)
    db = catalog(*tables)
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(
        await profile({"account_key": IntegerScalar(value=42)}), db, scope=scope_for(db)
    )
    field = result.fields[0]
    assert field.candidates[0].target.table_id == a.table_id
    assert field.candidates[0].confidence == Decimal(1)
    assert "FK_STRATEGY_REQUIRED" in field.explanations[0].blockers
    assert field.status == "review"
