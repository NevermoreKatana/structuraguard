"""Положительные стратегии ключей/связей и отказ от недоказанных арбитров upsert."""

import pytest
from tests.fakes.mapping import catalog, column, scope_for, table
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ConstraintInspectionMetadata,
    ForeignKeyCatalog,
    IndexCatalog,
    IndexKeyCatalog,
)
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.mapping_rules import MappingIdentity, MappingRelation
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


async def test_database_generated_identity_is_insert_only() -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    pk, amount = target.columns
    assert pk.inspection is not None
    pk = pk.model_copy(
        update={
            "writable": False,
            "inspection": pk.inspection.model_copy(update={"identity": "always"}),
        }
    )
    db = catalog(target.model_copy(update={"columns": (pk, amount)}))
    plan = replan(
        plan, database_fingerprint=db.database_fingerprint, mappings=(plan.mappings[1],)
    )
    validator = MappingPlanValidator(policy=policy)
    accepted = await validator.validate(plan, manifest, db, profile=profile)
    assert accepted.decision is ValidationDecision.ACCEPTED
    rejected = await validator.validate(
        replan(plan, operation=LoadOperation.UPSERT), manifest, db, profile=profile
    )
    assert "MAPPING_IDENTITY_REQUIRED" in {i.code for i in rejected.issues}


@pytest.mark.parametrize(
    "kind", ["partial", "expression", "invalid", "deferred", "natural"]
)
async def test_unsafe_unique_evidence_cannot_authorize_upsert(kind: str) -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    assert target.inspection is not None
    index = IndexCatalog(
        index_id="unique_amount",
        name="unique_amount",
        keys=(IndexKeyCatalog(column_id="amount"),),
        unique=True,
        origin="index",
    )
    updates: dict[str, object] = {}
    if kind == "partial":
        index = index.model_copy(update={"predicate": "amount > 0"})
    elif kind == "expression":
        index = index.model_copy(
            update={"keys": (IndexKeyCatalog(expression="amount + 1"),)}
        )
    elif kind == "invalid":
        index = index.model_copy(update={"valid": False})
    elif kind == "deferred":
        updates["constraints"] = (
            ConstraintInspectionMetadata(
                name="amount_key",
                kind="unique",
                column_ids=("amount",),
                deferrable=True,
            ),
        )
    updates["indexes"] = (index,)
    db = catalog(
        target.model_copy(
            update={"inspection": target.inspection.model_copy(update=updates)}
        )
    )
    plan = replan(
        plan,
        database_fingerprint=db.database_fingerprint,
        operation=LoadOperation.UPSERT,
        schema_version="1.1.0",
        identities=(
            MappingIdentity(
                table_id=target.table_id,
                kind="natural_key" if kind == "natural" else "explicit",
                column_ids=("amount",),
            ),
        ),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert "MAPPING_UPSERT_KEY_INVALID" in {i.code for i in result.issues}


async def test_unrelated_catalog_cycle_does_not_block_selected_plan() -> None:
    plan, manifest, db, policy, profile = await case()
    other = table(
        "other",
        column("value", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="self",
                column_ids=("value",),
                referenced_table_id="public.other",
                referenced_column_ids=("value",),
            ),
        ),
    )
    db = catalog(db.schemas[0].tables[0], other)
    plan = replan(plan, database_fingerprint=db.database_fingerprint)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert result.decision is ValidationDecision.ACCEPTED


async def test_complete_mapped_parent_relation_and_wrong_pair() -> None:
    plan, manifest, db, policy, profile = await case()
    parent_pk = db.schemas[0].tables[0].columns[0]
    parent = table("parent", column("id", "integer")).model_copy(
        update={"columns": (parent_pk,), "primary_key": ("id",)}
    )
    child = table(
        "child",
        column("id", "integer"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="parent",
                column_ids=("id",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("id",),
            ),
        ),
    ).model_copy(update={"columns": (parent_pk,), "primary_key": ("id",)})
    db = catalog(parent, child)
    first, second = plan.mappings
    mappings = (
        first.model_copy(
            update={
                "target": CatalogColumnRef(table_id=parent.table_id, column_id="id")
            }
        ),
        second.model_copy(
            update={"target": CatalogColumnRef(table_id=child.table_id, column_id="id")}
        ),
    )
    policy = policy.model_copy(
        update={
            "scope": scope_for(db),
            "allow_tables": (("public", "parent"), ("public", "child")),
            "source_identity_allow": tuple(m.target for m in mappings),
        }
    )
    relation = MappingRelation(
        foreign_key_id="parent",
        child_table_id=child.table_id,
        parent_table_id=parent.table_id,
        child_column_ids=("id",),
        parent_column_ids=("id",),
        child_sources=(second.source,),
        parent_sources=(first.source,),
        strategy="mapped_parent",
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        mappings=mappings,
        database_fingerprint=db.database_fingerprint,
        relations=(relation,),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert result.decision is ValidationDecision.ACCEPTED
    bad = replan(
        plan,
        relations=(relation.model_copy(update={"parent_column_ids": ("absent",)}),),
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.evidence is not None
    assert result.evidence.load_order == (parent.table_id, child.table_id)
    rejected = await MappingPlanValidator(policy=policy).validate(
        bad, manifest, db, profile=profile
    )
    assert "MAPPING_FK_PAIR_MISMATCH" in {i.code for i in rejected.issues}


async def test_lookup_allow_does_not_override_parent_column_deny() -> None:
    plan, manifest, db, policy, profile = await case()
    original = db.schemas[0].tables[0]
    parent = table("parent", column("id", "integer")).model_copy(
        update={"columns": (original.columns[0],), "primary_key": ("id",)}
    )
    fk = ForeignKeyCatalog(
        foreign_key_id="parent",
        column_ids=("amount",),
        referenced_table_id=parent.table_id,
        referenced_column_ids=("id",),
    )
    db = catalog(original.model_copy(update={"foreign_keys": (fk,)}), parent)
    parent_ref = CatalogColumnRef(table_id=parent.table_id, column_id="id")
    policy = policy.model_copy(
        update={
            "scope": scope_for(db),
            "allow_tables": (("public", "orders"), ("public", "parent")),
            "lookup_allow": (parent_ref,),
        }
    )
    relation = MappingRelation(
        foreign_key_id="parent",
        child_table_id=original.table_id,
        parent_table_id=parent.table_id,
        child_column_ids=("amount",),
        parent_column_ids=("id",),
        child_sources=(plan.mappings[1].source,),
        strategy="source_values",
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        relations=(relation,),
        database_fingerprint=db.database_fingerprint,
    )
    accepted = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert accepted.decision is ValidationDecision.ACCEPTED
    denied = policy.model_copy(
        update={"scope": policy.scope.model_copy(update={"deny": (parent_ref,)})}
    )
    result = await MappingPlanValidator(policy=denied).validate(
        plan, manifest, db, profile=profile
    )
    assert "MAPPING_COLUMN_DENIED" in {i.code for i in result.issues}
