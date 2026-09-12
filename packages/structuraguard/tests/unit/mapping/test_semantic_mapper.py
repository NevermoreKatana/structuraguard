"""Semantic выбор проверяется на настоящих M7/M8/M9 snapshots и scripted LLM."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import decision_for, mapping_options, run_mapper

from structuraguard.contracts.common import IntegerScalar, PipelineStatus
from structuraguard.contracts.database import ForeignKeyCatalog
from structuraguard.exceptions import LLMProviderError, MappingError
from structuraguard.mapping import prepare_semantic_mapping

pytestmark = pytest.mark.anyio


async def test_unambiguous_choice_keeps_sdk_score_and_provider_metadata() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    result, provider, scanner = await run_mapper(
        data, db, decision_for(prepared.groups[0])
    )
    group = result.groups[0]
    assert result.status is PipelineStatus.COMPLETED
    assert group.confidence == Decimal("0.998000")
    assert group.choices[0].scores[0].llm_score == Decimal("0.990000")
    assert group.calls[0].provider_id == "fake"
    assert group.calls[0].model_id == "scripted"
    assert group.calls[0].prompt is not None
    assert group.prompt.fingerprint == group.calls[0].prompt.fingerprint
    assert provider.call_count == 1
    assert "person@example.org" not in scanner.payloads[0]


async def test_same_column_names_are_disambiguated_by_table_candidate() -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0], preferred_table="public.suppliers")
    result, _, _ = await run_mapper(data, db, decision)
    group = result.groups[0]
    assert group.decision is not None
    selected = group.decision.columns[0].selected_candidate_id
    candidate = next(c for c in group.candidates.columns if c.candidate_id == selected)
    assert candidate.mapping.target.table_id == "public.suppliers"
    assert not group.ambiguous


@pytest.mark.parametrize("composite", [False, True])
async def test_entity_split_requires_complete_related_table_candidate(
    composite: bool,
) -> None:
    keys = ("customer_key", "tenant_key") if composite else ("customer_key",)
    refs = ("customer_ref", "tenant_ref") if composite else ("customer_ref",)
    data = await profile(
        {name: IntegerScalar(value=42) for name in (*keys, *refs, "quantity")}
    )
    db = catalog(
        table(
            "customers", *(column(n, "integer", position=i) for i, n in enumerate(keys))
        ),
        table(
            "orders",
            *(
                column(n, "integer", position=i)
                for i, n in enumerate((*refs, "quantity"))
            ),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="customer_fk",
                    column_ids=refs,
                    referenced_table_id="public.customers",
                    referenced_column_ids=keys,
                ),
            ),
        ),
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    assert len(group.relations) == 1
    assert len(group.relations[0].pairs) == len(keys)
    result, _, _ = await run_mapper(data, db, decision_for(group, split=True))
    assert result.groups[0].decision is not None
    assert len(result.groups[0].decision.tables[0].selected_candidate_ids) == 2
    assert result.groups[0].decision.relations[0].selected_candidate_id is not None
    assert (
        result.groups[0].candidates.relations[0].foreign_key.parent_column_ids == keys
    )


async def test_unknown_target_is_rejected_without_partial_decision() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0])
    decision = decision.model_copy(
        update={
            "columns": (
                decision.columns[0].model_copy(
                    update={"selected_candidate_id": "c999"}
                ),
            )
        }
    )
    with pytest.raises(MappingError, match="SEMANTIC_MAPPING_DECISION_INVALID"):
        await run_mapper(data, db, decision)


@pytest.mark.parametrize("raw", ['{"schema_version":', '{"unexpected":"value"}'])
async def test_malformed_structured_response_is_not_repaired(raw: str) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    with pytest.raises(LLMProviderError):
        await run_mapper(data, db, raw)


async def test_split_along_three_tables_preserves_two_distinct_relation_choices() -> (
    None
):
    names = ("customer_key", "customer_ref", "order_key", "order_ref")
    data = await profile({name: IntegerScalar(value=42) for name in names})
    db = catalog(
        table("customers", column("customer_key", "integer")),
        table(
            "orders",
            column("customer_ref", "integer"),
            column("order_key", "integer", position=1),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="customer_fk",
                    column_ids=("customer_ref",),
                    referenced_table_id="public.customers",
                    referenced_column_ids=("customer_key",),
                ),
            ),
        ),
        table(
            "items",
            column("order_ref", "integer"),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="order_fk",
                    column_ids=("order_ref",),
                    referenced_table_id="public.orders",
                    referenced_column_ids=("order_key",),
                ),
            ),
        ),
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    result, _, _ = await run_mapper(
        data, db, decision_for(prepared.groups[0], split=True)
    )
    decision = result.groups[0].decision
    assert decision is not None
    assert len(decision.tables[0].selected_candidate_ids) == 3
    assert len(decision.relations) == 2
    assert all(r.selected_candidate_id for r in decision.relations)


async def test_composite_relation_cannot_ignore_one_source_component() -> None:
    data = await profile(
        {
            n: IntegerScalar(value=42)
            for n in ("parent_key", "tenant_key", "parent_ref", "tenant_ref")
        }
    )
    db = catalog(
        table(
            "parents",
            column("parent_key", "integer"),
            column("tenant_key", "integer", position=1),
        ),
        table(
            "children",
            column("parent_ref", "integer"),
            column("tenant_ref", "integer", position=1),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="composite",
                    column_ids=("parent_ref", "tenant_ref"),
                    referenced_table_id="public.parents",
                    referenced_column_ids=("parent_key", "tenant_key"),
                ),
            ),
        ),
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0], split=True)
    missing = next(
        f.source_id
        for f in prepared.groups[0].fields
        if f.ranked.source.field_name == "tenant_ref"
    )
    decision = decision.model_copy(
        update={
            "columns": tuple(
                c.model_copy(
                    update={"status": "unmapped", "selected_candidate_id": None}
                )
                if c.source_id == missing
                else c
                for c in decision.columns
            )
        }
    )
    with pytest.raises(
        MappingError, match="SEMANTIC_MAPPING_DECISION_INVALID"
    ) as error:
        await run_mapper(data, db, decision)
    assert error.value.details["reason"] == "incomplete_composite_relation"
