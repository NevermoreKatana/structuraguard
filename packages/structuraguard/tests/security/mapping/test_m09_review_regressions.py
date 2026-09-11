"""Регрессии финального review: недоверенные anchors и metadata вне scope."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, profile, scope_for, table

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.database import CatalogColumnRef
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingWeights,
)
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.exceptions import MappingError
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("column_name", ["customer_code", "cust\u043emer_code"])
async def test_confusable_target_alias_cannot_resolve_neighbor_ambiguity(
    column_name: str,
) -> None:
    data = await profile(
        {
            "customer_code": StringScalar(value="stable-code"),
            "email": StringScalar(value="a@example.org"),
        }
    )
    db = catalog(
        table("customers", column(column_name), column("email", position=1)),
        table("contacts", column("email")),
    )
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                columns=(
                    SemanticColumn(column_name=column_name, aliases=("customer_code",)),
                ),
            ),
        ),
    )
    weights = MappingWeights(
        name_similarity=Decimal("0.9"),
        alias_match=Decimal(0),
        type_compatibility=Decimal(0),
        value_pattern_match=Decimal(0),
        structural_context=Decimal("0.1"),
        database_relation_score=Decimal(0),
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=weights)
    ).rank(data, db, scope=scope_for(db), semantic_catalog=semantic)
    email = next(f for f in result.fields if f.source.field_name == "email")
    if column_name == "customer_code":
        assert email.status == "auto_candidate"
        assert email.candidates[0].target.table_id == "public.customers"
        assert email.candidates[0].confidence == 1
        assert email.gap == Decimal("0.1")
        return

    code = next(f for f in result.fields if f.source.field_name == "customer_code")
    assert "CONFUSABLE_NAME" in code.explanations[0].blockers
    assert email.status == "review" and email.ambiguous and email.gap == 0
    assert {c.target.table_id for c in email.candidates} == {
        "public.customers",
        "public.contacts",
    }
    for explanation in email.explanations:
        assert "AMBIGUOUS_TARGET" in explanation.blockers
        assert (
            next(s.value for s in explanation.signals if s.code == "structural_context")
            == 0
        )


@pytest.mark.parametrize("scope_mode", ["omitted", "denied", "empty", "allowed"])
@pytest.mark.parametrize("metadata", ["table_name", "table_alias"])
async def test_table_name_limits_apply_only_to_candidate_scope(
    scope_mode: str, metadata: str
) -> None:
    # Валидный 34-байтовый SQL identifier превышает только лимит tokenizer.
    payload = "a1" * 17
    excluded_name = payload if metadata == "table_name" else "excluded"
    good = table("good", column("email"))
    excluded = table(excluded_name, column("unrelated"))
    db = catalog(good, excluded)
    good_ref = CatalogColumnRef(table_id=good.table_id, column_id="email")
    excluded_ref = CatalogColumnRef(table_id=excluded.table_id, column_id="unrelated")
    scope = scope_for(db)
    if scope_mode == "omitted":
        scope = scope.model_copy(update={"allow": (good_ref,)})
    elif scope_mode == "denied":
        scope = scope.model_copy(update={"deny": (excluded_ref,)})
    elif scope_mode == "empty":
        scope = scope.model_copy(update={"allow": ()})
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name=excluded_name,
                aliases=(payload,) if metadata == "table_alias" else (),
            ),
        ),
    )
    data = await profile()
    if scope_mode == "allowed":
        with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED"):
            await DeterministicMapper().rank(
                data, db, scope=scope, semantic_catalog=semantic
            )
        return

    result = await DeterministicMapper().rank(
        data, db, scope=scope, semantic_catalog=semantic
    )
    field = result.fields[0]
    if scope_mode == "empty":
        assert field.status == "unmapped" and not field.candidates
        assert "NO_ADMISSIBLE_CANDIDATES" in field.reasons
    else:
        assert tuple(c.target for c in field.candidates) == (good_ref,)
