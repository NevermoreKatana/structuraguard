"""Ранжирование не превращает сходство имени в разрешение на запись."""

from decimal import Decimal, localcontext

import pytest
from tests.fakes.mapping import (
    catalog,
    column,
    only_weight,
    profile,
    refs,
    rehash_profile,
    table,
)

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
    MappingWeights,
)
from structuraguard.contracts.profiling import ProfileLabel
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.mapping import DeterministicMapper

pytestmark = pytest.mark.anyio


async def test_wrong_type_cannot_win_by_exact_name() -> None:
    data = await profile()
    db = catalog(
        table("wrong", column("email", "integer")), table("right", column("email"))
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
    assert [c.target.table_id for c in field.candidates] == ["public.right"]
    explanation = field.explanations[0]
    signals = {s.code: s for s in explanation.signals}
    assert signals["exact_name"].value == 1
    assert signals["normalized_name"].value > 0
    assert signals["type_compatibility"].value == 1
    assert explanation.final_score == field.candidates[0].confidence
    assert (
        field.candidates[0].normalized_fingerprint
        == data.normalized_manifest_fingerprint
    )
    assert "TYPE_INCOMPATIBLE" in field.reasons


async def test_top_one_preserves_ambiguity_and_stable_tie() -> None:
    data = await profile()
    db = catalog(table("z", column("email")), table("a", column("email")))
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )
    mapper = DeterministicMapper(DeterministicMappingOptions(top_k=1))
    first = await mapper.rank(data, db, scope=scope)
    with localcontext() as ctx:
        ctx.prec = 2
        second = await mapper.rank(data, db, scope=scope)
    assert first == second
    field = first.fields[0]
    assert len(field.candidates) == 1
    assert field.candidates[0].target.table_id == "public.a"
    assert field.ambiguous and field.gap == 0 and field.competitor_count == 2
    assert field.status != "auto_candidate"


async def test_empty_scope_and_unrelated_names_are_explicitly_unmapped() -> None:
    data = await profile({"weather": StringScalar(value="sunny")})
    db = catalog(table("customers", column("legal_name")))
    for allowed in ((), refs(db)):
        result = await DeterministicMapper().rank(
            data,
            db,
            scope=MappingScope(
                target_id=db.target_id,
                target_policy_fingerprint=db.target_policy_fingerprint,
                allow=allowed,
            ),
        )
        assert result.fields[0].status == "unmapped"
        assert not result.fields[0].candidates


@pytest.mark.parametrize("name", ["postal_codde", "postalcode", "postal-code"])
async def test_similar_names_offer_allowed_compatible_candidates(name: str) -> None:
    data = await profile({name: StringScalar(value="101000")})
    db = catalog(table("contacts", column("postal_code")))
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
    assert field.candidates[0].target.column_id == "postal_code"
    assert not field.ambiguous
    assert field.explanations[0].signals[3].code == "name_similarity"
    assert field.explanations[0].signals[3].value > Decimal("0.8")


async def test_equal_typo_candidates_remain_ambiguous() -> None:
    data = await profile({"postal_codde": StringScalar(value="101000")})
    db = catalog(
        table("contacts", column("postal_code")),
        table("addresses", column("postal_code")),
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
    assert len(field.candidates) == 2
    assert field.ambiguous and field.gap == 0
    assert field.status != "auto_candidate"


@pytest.mark.parametrize("header", ["email", "email_0", None])
async def test_observed_header_takes_priority_over_generated_semantic_name(
    header: str | None,
) -> None:
    data = await profile({"email_0": StringScalar(value="person@example.org")})
    original = data.fields[0]
    if header is not None:
        labelled = original.model_copy(
            update={
                "labels": (
                    ProfileLabel(
                        field=original.field,
                        kind="source_name",
                        text=header,
                        origin="observed_location",
                    ),
                ),
                "context_available": True,
            }
        )
        data = rehash_profile(data, fields=(labelled,))
    db = catalog(table("contacts", column("email"), column("email_0", position=1)))
    mapper = DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    )
    result = await mapper.rank(
        data,
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    field = result.fields[0]
    assert field.source == original.field
    assert field.candidates[0].target.column_id == (header or "email_0")
    assert not field.ambiguous


async def test_russian_aliases_are_scoped_and_do_not_hide_competitors() -> None:
    data = await profile({"Контрагент": StringScalar(value="ООО Альфа")})
    db = catalog(
        table("customers", column("legal_name")),
        table("suppliers", column("legal_name")),
    )
    scope = MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
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
                    SemanticColumn(
                        column_name="legal_name", aliases=("Контрагент", "Customer")
                    ),
                ),
            ),
        ),
    )
    result = await DeterministicMapper().rank(
        data, db, scope=scope, semantic_catalog=semantic
    )
    field = result.fields[0]
    assert field.candidates[0].target.table_id == "public.customers"
    assert (
        next(s.value for s in field.explanations[0].signals if s.code == "alias_match")
        == 1
    )


def test_weights_reject_negative_nonfinite_float_and_nonunit_sum() -> None:
    from pydantic import ValidationError

    for value in (
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("0.7"),
        0.35,
        Decimal("1e-999999"),
    ):
        with pytest.raises(ValidationError):
            MappingWeights.model_validate({"name_similarity": value})
