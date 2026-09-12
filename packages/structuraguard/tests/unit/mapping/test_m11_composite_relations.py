"""Полные composite FK, закрытые стратегии lookup и доказательства identity."""

import pytest
from tests.fakes.mapping import catalog, column, scope_for, table
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import CatalogColumnRef, ForeignKeyCatalog
from structuraguard.contracts.mapping import MappingPlanValidationResult
from structuraguard.contracts.mapping_rules import MappingIdentity, MappingRelation
from structuraguard.mapping import MappingPlanValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("source_values", None),
        ("lookup", None),
        ("parent_order", "MAPPING_FK_PAIR_MISMATCH"),
        ("child_order", "MAPPING_FK_PAIR_MISMATCH"),
        ("source_order", "MAPPING_FK_PAIR_MISMATCH"),
        ("partial", "MAPPING_FK_PAIR_MISMATCH"),
        ("missing_lookup", "MAPPING_RELATION_UNRESOLVED"),
        ("missing_unique", "MAPPING_RELATION_UNRESOLVED"),
        ("generated_key", "MAPPING_RELATION_STRATEGY_UNSUPPORTED"),
        ("deferred", "MAPPING_RELATION_STRATEGY_UNSUPPORTED"),
        ("two_phase", "MAPPING_RELATION_STRATEGY_UNSUPPORTED"),
    ],
)
async def test_composite_fk_needs_exact_pairs_key_and_supported_strategy(
    variant: str, code: str | None
) -> None:
    plan, manifest, db, policy, profile = await case()
    parent = table(
        "parent",
        *(
            column(name, "integer", position=i).model_copy(update={"nullable": False})
            for i, name in enumerate(("a", "b"))
        ),
    )
    parent = parent.model_copy(
        update={
            "unique_constraints": () if variant == "missing_unique" else (("a", "b"),)
        }
    )
    child = db.schemas[0].tables[0]
    fk = ForeignKeyCatalog(
        foreign_key_id="composite",
        column_ids=("id", "amount"),
        referenced_table_id=parent.table_id,
        referenced_column_ids=("a", "b"),
    )
    db = catalog(child.model_copy(update={"foreign_keys": (fk,)}), parent)
    policy = policy.model_copy(
        update={
            "scope": scope_for(db),
            "allow_tables": (("public", "orders"), ("public", "parent")),
            "lookup_allow": ()
            if variant == "missing_lookup"
            else tuple(
                CatalogColumnRef(table_id=parent.table_id, column_id=c)
                for c in ("a", "b")
            ),
        }
    )
    relation = MappingRelation(
        foreign_key_id=fk.foreign_key_id,
        child_table_id=child.table_id,
        parent_table_id=parent.table_id,
        child_column_ids=("id", "amount"),
        parent_column_ids=("a", "b"),
        child_sources=tuple(m.source for m in plan.mappings),
        strategy="source_values",
    )
    changes: dict[str, object] = {}
    if variant in {"lookup", "generated_key", "deferred", "two_phase"}:
        changes["strategy"] = variant
    elif variant == "parent_order":
        changes["parent_column_ids"] = ("b", "a")
    elif variant == "child_order":
        changes["child_column_ids"] = ("amount", "id")
    elif variant == "source_order":
        changes["child_sources"] = tuple(reversed(relation.child_sources))
    elif variant == "partial":
        changes.update(
            child_column_ids=("id",),
            parent_column_ids=("a",),
            child_sources=(relation.child_sources[0],),
        )
    relation = MappingRelation.model_validate(
        {**relation.model_dump(mode="python"), **changes}
    )
    plan = replan(
        plan,
        schema_version="1.1.0",
        database_fingerprint=db.database_fingerprint,
        relations=(relation,),
    )
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    if code is None:
        assert result.decision is ValidationDecision.ACCEPTED
        assert result.validated_plan is not None
        assert result.evidence is not None and result.evidence.load_order == (
            child.table_id,
        )
    else:
        assert code in {i.code for i in result.issues}
        assert result.decision is ValidationDecision.REJECTED
        assert result.validated_plan is None


@pytest.mark.parametrize("variant", ["nullable", "ambiguous", "incomplete"])
async def test_unproven_identity_never_uses_profile_uniqueness_as_constraint(
    variant: str,
) -> None:
    plan, manifest, db, policy, profile = await case()
    target = db.schemas[0].tables[0]
    updates: dict[str, object] = {"unique_constraints": (("amount",),)}
    if variant == "ambiguous":
        updates.update(
            primary_key=(),
            unique_constraints=(("amount",), ("id", "amount")),
            columns=tuple(
                c.model_copy(update={"nullable": False, "primary_key": False})
                for c in target.columns
            ),
        )
        policy = policy.model_copy(update={"source_identity_allow": ()})
    elif variant == "incomplete":
        updates["unique_constraints"] = (("id", "amount"),)
    db = catalog(target.model_copy(update=updates))
    changes: dict[str, object] = {
        "schema_version": "1.1.0",
        "database_fingerprint": db.database_fingerprint,
        "operation": LoadOperation.UPSERT,
    }
    if variant != "ambiguous":
        changes["identities"] = (
            MappingIdentity(
                table_id=target.table_id,
                kind="explicit",
                column_ids=("amount",) if variant == "nullable" else ("id", "amount"),
            ),
        )
    if variant == "incomplete":
        changes["mappings"] = (plan.mappings[0],)
    result = await MappingPlanValidator(policy=policy).validate(
        replan(plan, **changes), manifest, db, profile=profile
    )
    assert isinstance(result, MappingPlanValidationResult)
    assert result.validated_plan is None
    expected = {
        "nullable": "MAPPING_IDENTITY_NULLABLE",
        "ambiguous": "MAPPING_IDENTITY_AMBIGUOUS",
        "incomplete": "MAPPING_UPSERT_KEY_INVALID",
    }[variant]
    assert expected in {i.code for i in result.issues}
    assert result.decision is (
        ValidationDecision.NEEDS_REVIEW
        if variant == "ambiguous"
        else ValidationDecision.REJECTED
    )
