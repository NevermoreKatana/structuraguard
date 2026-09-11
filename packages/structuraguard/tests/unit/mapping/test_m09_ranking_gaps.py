"""Наблюдаемая приёмка конфигурации, неоднозначности и полноты результата M9."""

from decimal import Decimal, localcontext

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

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingWeights,
)
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("alias", ["Контрагент", "Покупатель", "Customer"])
async def test_same_scoped_alias_keeps_both_targets_in_review(alias: str) -> None:
    db = catalog(
        table("suppliers", column("legal_name")),
        table("customers", column("legal_name")),
    )
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=tuple(
            SemanticTable(
                schema_name="public",
                table_name=t,
                columns=(SemanticColumn(column_name="legal_name", aliases=(alias,)),),
            )
            for t in ("suppliers", "customers")
        ),
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(
            weights=only_weight("alias_match"),
        )
    ).rank(
        await profile({alias: StringScalar(value="Acme")}),
        db,
        scope=scope_for(db),
        semantic_catalog=semantic,
    )
    field = result.fields[0]
    assert [c.target.table_id for c in field.candidates] == [
        "public.customers",
        "public.suppliers",
    ]
    assert field.ambiguous and field.gap == 0 and field.tie_count == 2
    assert field.status == "review"
    assert all("AMBIGUOUS_TARGET" in e.blockers for e in field.explanations)


async def test_two_source_fields_keep_collision_without_greedy_reassignment() -> None:
    data = await profile(
        {
            "email": StringScalar(value="a@example.org"),
            "EMAIL": StringScalar(value="b@example.org"),
        }
    )
    db = catalog(table("customers", column("email")))
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(data, db, scope=scope_for(db))
    assert len(result.fields) == 2
    for field in result.fields:
        assert field.candidates[0].target.column_id == "email"
        assert "TARGET_COLLISION" in field.reasons
        assert field.explanations[0].validation_penalty == Decimal("0.10")
        assert field.status == "review"
        assert not field.ambiguous and field.gap is None


async def test_same_field_name_in_distinct_entities_is_not_merged() -> None:
    data = await profile(
        {"email": StringScalar(value="a@example.org")}, entity="customers"
    )
    first = data.fields[0]
    other = SemanticFieldRef(entity_type="suppliers", field_name="email")
    second = first.model_copy(
        update={"field": other, "pii": first.pii.model_copy(update={"field": other})}
    )
    data = rehash_profile(data, fields=(second, first))
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    result = await DeterministicMapper().rank(data, db, scope=scope_for(db))
    assert [f.source.entity_type for f in result.fields] == ["customers", "suppliers"]
    for entity in ("customers", "suppliers"):
        field = result.field(entity, "email")
        assert field.candidates[0].target.table_id == f"public.{entity}"
        assert all(c.source == field.source for c in field.candidates)
        assert "TARGET_COLLISION" not in field.reasons


@pytest.mark.parametrize(
    ("weight", "ambiguous"), [("0.099999", True), ("0.100000", False)]
)
async def test_margin_uses_hidden_runner_up_at_exact_boundary(
    weight: str, ambiguous: bool
) -> None:
    w = Decimal(weight)
    options = DeterministicMappingOptions(
        top_k=1,
        weights=MappingWeights(
            name_similarity=1 - w,
            alias_match=Decimal(0),
            type_compatibility=Decimal(0),
            value_pattern_match=Decimal(0),
            structural_context=w,
            database_relation_score=Decimal(0),
        ),
    )
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    result = await DeterministicMapper(options).rank(
        await profile(entity="customers"), db, scope=scope_for(db)
    )
    field = result.fields[0]
    assert len(field.candidates) == 1 and field.competitor_count == 2
    assert field.gap == w and field.ambiguous == ambiguous
    assert field.status == ("review" if ambiguous else "auto_candidate")
    assert field.explanations[0].ambiguity_penalty == (
        Decimal("0.1") if ambiguous else 0
    )


async def test_context_winner_survives_top_one_and_weights_change_ranking() -> None:
    data = await profile({"Customer Name": StringScalar(value="Acme")}, entity="orders")
    db = catalog(
        table("wrong", column("Customer Name")),
        table("orders", column("customer_name")),
    )
    contextual = await DeterministicMapper(DeterministicMappingOptions(top_k=1)).rank(
        data, db, scope=scope_for(db)
    )
    lexical = await DeterministicMapper(
        DeterministicMappingOptions(top_k=1, weights=only_weight("name_similarity"))
    ).rank(data, db, scope=scope_for(db))
    assert contextual.fields[0].candidates[0].target.table_id == "public.orders"
    assert lexical.fields[0].candidates[0].target.table_id == "public.wrong"
    assert contextual.fields[0].competitor_count == 2


async def test_name_only_weight_cannot_restore_hard_type_mismatch() -> None:
    db = catalog(
        table("wrong", column("email", "integer")), table("right", column("email"))
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(await profile(), db, scope=scope_for(db))
    assert [c.target.table_id for c in result.fields[0].candidates] == ["public.right"]
    assert "TYPE_INCOMPATIBLE" in result.fields[0].reasons


async def test_sdk_breakdown_reconstructs_final_score_and_preserves_inputs() -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    scope = scope_for(db)
    before = tuple(x.canonical_json() for x in (data, db, scope))
    result = await DeterministicMapper().rank(data, db, scope=scope)
    assert before == tuple(x.canonical_json() for x in (data, db, scope))
    assert result.profile_fingerprint == data.profile_fingerprint
    assert result.normalized_data_fingerprint == data.normalized_data_fingerprint
    assert result.scope_fingerprint == scope.fingerprint
    for field in result.fields:
        for candidate, explanation in zip(
            field.candidates, field.explanations, strict=True
        ):
            signals = {s.code: s for s in explanation.signals}
            assert set(signals) == {
                "exact_name",
                "normalized_name",
                "transliterated_name",
                "name_similarity",
                "alias_match",
                "type_compatibility",
                "value_pattern_match",
                "structural_context",
                "database_relation_score",
            }
            with localcontext() as ctx:
                ctx.prec = 40
                for signal in signals.values():
                    assert signal.contribution == signal.value * signal.weight
                base = sum(
                    (s.contribution for s in signals.values()), Decimal(0)
                ).quantize(Decimal("0.000001"))
                assert explanation.base_score == base
                assert candidate.confidence == max(
                    Decimal(0),
                    base
                    - explanation.ambiguity_penalty
                    - explanation.validation_penalty
                    - explanation.security_penalty,
                )
            assert candidate.source_fingerprint == data.source.source_fingerprint
            assert (
                candidate.normalized_fingerprint == data.normalized_manifest_fingerprint
            )
            assert candidate.database_fingerprint == db.database_fingerprint
            assert candidate.target_policy_fingerprint == db.target_policy_fingerprint
            assert candidate.producer.component_id == "mapping.deterministic"


@pytest.mark.parametrize("name", ["раypal", "Date", "Name"])
async def test_confusable_or_generic_exact_name_cannot_auto_map(name: str) -> None:
    db = catalog(table("target", column(name)))
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(
        await profile({name: StringScalar(value="ordinary text")}),
        db,
        scope=scope_for(db),
    )
    field = result.fields[0]
    assert field.candidates[0].confidence == 1
    assert field.status == "review"
    assert (
        "CONFUSABLE_NAME" if name == "раypal" else "GENERIC_NAME_ONLY"
    ) in field.explanations[0].blockers


@pytest.mark.parametrize("name", ["Статус", "Status", "Дата", "Date"])
async def test_bilingual_common_names_remain_ambiguous_across_schemas(
    name: str,
) -> None:
    db = catalog(
        table("orders", column(name), schema="crm"),
        table("orders", column(name), schema="archive"),
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(
        await profile({name: StringScalar(value="ordinary text")}),
        db,
        scope=scope_for(db),
    )
    field = result.fields[0]
    assert field.candidates[0].target.table_id == "archive.orders"
    assert field.ambiguous and field.gap == 0 and field.status == "review"
