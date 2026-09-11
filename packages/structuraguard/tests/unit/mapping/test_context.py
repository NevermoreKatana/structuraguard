"""Соседи и составной FK дают evidence, не разрешение на загрузку."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, profile, refs, table

from structuraguard.contracts.common import IntegerScalar, StringScalar
from structuraguard.contracts.database import ForeignKeyCatalog
from structuraguard.contracts.deterministic_mapping import MappingScope
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import FieldRelationship, NormalizedDataProfile
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


async def test_entity_context_distinguishes_same_column_names() -> None:
    data = await profile({"amount": StringScalar(value="text")}, entity="Заказы")
    db = catalog(
        table("customers", column("amount")), table("orders", column("amount"))
    )
    result = await DeterministicMapper().rank(
        data,
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    field = result.fields[0]
    assert field.candidates[0].target.table_id == "public.orders"
    assert field.explanations[0].base_score > field.explanations[1].base_score
    assert (
        next(
            s.value
            for s in field.explanations[0].signals
            if s.code == "structural_context"
        )
        > 0
    )


async def composite_profile() -> NormalizedDataProfile:
    data = await profile(
        {
            name: IntegerScalar(value=42)
            for name in ("tenant_key", "account_key", "tenant_ref", "account_ref")
        }
    )
    fields = []
    for f in data.fields:
        ref = SemanticFieldRef(
            entity_type="parent" if f.field.field_name.endswith("key") else "child",
            field_name=f.field.field_name,
        )
        fields.append(
            f.model_copy(
                update={"field": ref, "pii": f.pii.model_copy(update={"field": ref})}
            )
        )
    references = {f.field.field_name: f.field for f in fields}
    relationships = tuple(
        FieldRelationship(
            left=references[a], right=references[b], kind="parent_child", count=25
        )
        for a, b in (("tenant_key", "tenant_ref"), ("account_key", "account_ref"))
    )
    return NormalizedDataProfile.model_validate(
        data.model_copy(
            update={
                "fields": tuple(fields),
                "relationships": relationships,
                "profile_fingerprint": "sha256:" + "0" * 64,
            }
        ).model_dump(mode="python")
    )


@pytest.mark.parametrize("reversed_pairs", [False, True])
async def test_composite_graph_checks_every_ordered_pair(reversed_pairs: bool) -> None:
    data = await composite_profile()
    parent = table(
        "parents",
        column("tenant_key", "integer"),
        column("account_key", "integer", position=1),
    )
    child = table(
        "children",
        column("tenant_ref", "integer"),
        column("account_ref", "integer", position=1),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="composite",
                column_ids=("tenant_ref", "account_ref"),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("account_key", "tenant_key")
                if reversed_pairs
                else ("tenant_key", "account_key"),
            ),
        ),
    )
    db = catalog(parent, child)
    result = await DeterministicMapper().rank(
        data,
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    field = result.field("child", "tenant_ref")
    explanation = next(
        e
        for c, e in zip(field.candidates, field.explanations, strict=True)
        if c.target.table_id == child.table_id and c.target.column_id == "tenant_ref"
    )
    assert next(
        s.value for s in explanation.signals if s.code == "database_relation_score"
    ) == (Decimal("0.50") if reversed_pairs else Decimal(1))
    assert explanation.foreign_key_ids == ("composite",)
    assert "FK_UNRESOLVED" in explanation.blockers
    assert field.status != "auto_candidate"


@pytest.mark.parametrize(
    "kind",
    [
        "file_name",
        "sheet",
        "json_parent",
        "xml_parent",
        "html_heading",
        "pdf_section",
        "document_table",
        "path",
    ],
)
async def test_explicit_source_labels_select_table_without_io(kind: str) -> None:
    from structuraguard.contracts.profiling import ProfileLabel

    data = await profile()
    f = data.fields[0]
    label = ProfileLabel.model_validate(
        {
            "field": f.field,
            "kind": kind,
            "text": "orders.csv" if kind == "file_name" else "orders",
        }
    )
    data = NormalizedDataProfile.model_validate(
        data.model_copy(
            update={
                "fields": (
                    f.model_copy(
                        update={"labels": (label,), "context_available": True}
                    ),
                ),
                "profile_fingerprint": "sha256:" + "0" * 64,
            }
        ).model_dump(mode="python")
    )
    db = catalog(table("customers", column("email")), table("orders", column("email")))
    result = await DeterministicMapper().rank(
        data,
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    assert result.fields[0].candidates[0].target.table_id == "public.orders"
    assert not result.fields[0].ambiguous


async def test_unrelated_cycle_with_reused_fk_name_does_not_mark_valid_edge_cyclic() -> (
    None
):
    data = await composite_profile()
    parent = table(
        "parents",
        column("tenant_key", "integer"),
        column("account_key", "integer", position=1),
    )
    child = table(
        "children",
        column("tenant_ref", "integer"),
        column("account_ref", "integer", position=1),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="composite",
                column_ids=("tenant_ref", "account_ref"),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("tenant_key", "account_key"),
            ),
        ),
    )
    cyclic = table(
        "self",
        column("loop_key", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="composite",
                column_ids=("loop_key",),
                referenced_table_id="public.self",
                referenced_column_ids=("loop_key",),
            ),
        ),
    )
    db = catalog(parent, child, cyclic)
    result = await DeterministicMapper().rank(
        data,
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    field = result.field("child", "tenant_ref")
    for candidate, explanation in zip(
        field.candidates, field.explanations, strict=True
    ):
        if candidate.target.table_id == child.table_id:
            assert "FK_STRATEGY_REQUIRED" not in explanation.blockers
        if candidate.target.table_id == cyclic.table_id:
            assert "FK_STRATEGY_REQUIRED" in explanation.blockers
    assert any(c.target.table_id == child.table_id for c in field.candidates)
