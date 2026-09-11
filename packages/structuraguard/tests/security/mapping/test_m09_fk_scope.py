"""Сужение scope не превращает непроверенный FK в безопасную рекомендацию."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, profile, refs, table

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.database import CatalogColumnRef, ForeignKeyCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
    MappingWeights,
)
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("parent_scope", ["omitted", "denied", "read_only"])
async def test_out_of_scope_parent_cannot_remove_fk_blocker(parent_scope: str) -> None:
    parent = table("customers", column("customer_key", "integer"))
    if parent_scope == "read_only":
        parent = parent.model_copy(update={"writable": False})
    child = table(
        "orders",
        column("customer_key", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="customer_fk",
                column_ids=("customer_key",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("customer_key",),
            ),
        ),
    )
    db = catalog(parent, child)
    target = CatalogColumnRef(table_id=child.table_id, column_id="customer_key")
    parent_ref = CatalogColumnRef(table_id=parent.table_id, column_id="customer_key")
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db) if parent_scope != "omitted" else (target,),
        deny=(parent_ref,) if parent_scope == "denied" else (),
    )
    weights = MappingWeights(
        name_similarity=Decimal(1),
        alias_match=Decimal(0),
        type_compatibility=Decimal(0),
        value_pattern_match=Decimal(0),
        structural_context=Decimal(0),
        database_relation_score=Decimal(0),
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=weights)
    ).rank(await profile({"customer_key": IntegerScalar(value=42)}), db, scope=scope)
    field = result.fields[0]
    assert tuple(c.target for c in field.candidates) == (target,)
    assert field.candidates[0].confidence == 1
    assert "FK_UNRESOLVED" in field.explanations[0].blockers
    assert field.status == "review"
    graph = next(
        s for s in field.explanations[0].signals if s.code == "database_relation_score"
    )
    assert graph.value == 0
