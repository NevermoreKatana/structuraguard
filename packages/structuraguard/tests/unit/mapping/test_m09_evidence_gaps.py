"""Отсутствующее и неполное evidence не превращается в auto recommendation."""

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
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar, NullScalar, StringScalar
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.profiling import LocalePolicy, NormalizedProfilingOptions
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticTable,
)
from structuraguard.mapping import DeterministicMapper
from structuraguard.mapping._compatibility import pattern_match, type_compatibility
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("locale", "value"),
    [
        (LocalePolicy.RU_RU, "01.09.2026"),
        (LocalePolicy.EN_US, "09/01/2026"),
        (LocalePolicy.EN_GB, "01/09/2026"),
        (LocalePolicy.UNSPECIFIED, "01/09/2026"),
    ],
)
async def test_localized_dates_remain_conditional_or_explicitly_ambiguous(
    locale: LocalePolicy, value: str
) -> None:
    data = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=locale)
    ).profile(normalized_stream([{"event_date": StringScalar(value=value)}] * 25))
    db = catalog(
        table("events", column("event_date", "date")),
        table("wrong", column("event_date", "integer")),
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(data, db, scope=scope_for(db))
    field = result.fields[0]
    assert [c.target.table_id for c in field.candidates] == ["public.events"]
    explanation = field.explanations[0]
    assert field.status == "review"
    if locale == LocalePolicy.UNSPECIFIED:
        assert explanation.compatibility == "unknown"
        assert "SOURCE_EVIDENCE_CONFLICT" in explanation.blockers
    else:
        assert explanation.compatibility == "conditional"
        assert "TRANSFORMATION_REQUIRED" in explanation.blockers


@pytest.mark.parametrize(
    ("value", "pattern"),
    [
        ("person@example.org", "email"),
        ("+7 999 123-45-67", "phone"),
        ("https://example.org/path", "url"),
        ("550e8400-e29b-41d4-a716-446655440000", "uuid"),
        ("7707083893", "russian_inn_10"),
        ("500100732259", "russian_inn_12"),
        ("125 000,50 RUB", "money"),
        ("125 000,50 RUB", "currency"),
        ("Описание обычного товара без специальных форматов", "free_text"),
        ("true", "boolean"),
    ],
)
async def test_patterns_require_target_semantics_and_full_evidence(
    value: str, pattern: str
) -> None:
    data = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=LocalePolicy.RU_RU)
    ).profile(normalized_stream([{"payload": StringScalar(value=value)}] * 25))
    evidence = pattern_match(data.fields[0], (pattern,))
    assert evidence.score == 1 and evidence.coverage == 1
    assert pattern_match(data.fields[0], ()).score == 0
    assert not pattern_match(data.fields[0], ()).available
    wrong = pattern_match(data.fields[0], ("email" if pattern != "email" else "phone",))
    assert wrong.score == 0 and "SEMANTIC_PATTERN_CONFLICT" in wrong.blockers


async def test_invalid_inn_checksum_never_receives_valid_inn_bonus() -> None:
    field = (await profile({"ИНН": StringScalar(value="7707083894")})).fields[0]
    evidence = pattern_match(field, ("russian_inn_10", "russian_inn_12"))
    assert evidence.score == 0
    assert "SEMANTIC_PATTERN_CONFLICT" in evidence.blockers


@pytest.mark.parametrize(
    "missing",
    [
        "labels",
        "null_only",
        "unknown_type",
        "pattern_skipped",
        "mixed_kinds",
        "pair_limit",
    ],
)
async def test_missing_evidence_is_visible_under_adversarial_weights(
    missing: str,
) -> None:
    if missing == "pattern_skipped":
        data = await NormalizedDataProfiler(
            NormalizedProfilingOptions(max_pattern_bytes=1)
        ).profile(
            normalized_stream([{"email": StringScalar(value="a@example.org")}] * 25)
        )
    elif missing == "mixed_kinds":
        data = await NormalizedDataProfiler().profile(
            normalized_stream(
                [{"email": StringScalar(value="a@example.org")}] * 24
                + [{"email": IntegerScalar(value=7)}]
            )
        )
    else:
        data = (
            await profile({"email": NullScalar()})
            if missing == "null_only"
            else await profile()
        )
    if missing == "pair_limit":
        data = rehash_profile(
            data,
            reasons=("pair_limit",),
            fields=(
                data.fields[0].model_copy(
                    update={
                        "field": data.fields[0].field.model_copy(
                            update={"entity_type": "customers"}
                        ),
                        "pii": data.fields[0].pii.model_copy(
                            update={
                                "field": data.fields[0].field.model_copy(
                                    update={"entity_type": "customers"}
                                )
                            }
                        ),
                    }
                ),
            ),
        )
    db = catalog(
        table(
            "customers",
            column("email", "unknown" if missing == "unknown_type" else "text"),
        )
    )
    result = await DeterministicMapper(
        DeterministicMappingOptions(weights=only_weight("name_similarity"))
    ).rank(data, db, scope=scope_for(db))
    field = result.fields[0]
    if missing == "mixed_kinds":
        assert field.status == "unmapped" and not field.candidates
        assert "TYPE_INCOMPATIBLE" in field.reasons
        return
    explanation = field.explanations[0]
    signals = {s.code: s for s in explanation.signals}
    if missing == "labels":
        assert (
            signals["structural_context"].value == 0
            and not signals["structural_context"].available
        )
        assert (
            signals["database_relation_score"].value == 0
            and not signals["database_relation_score"].available
        )
    else:
        assert field.status == "review" and explanation.blockers
    if missing in {"null_only", "unknown_type"}:
        assert explanation.compatibility == "unknown"
        assert not signals["type_compatibility"].available
    if missing == "pattern_skipped":
        assert signals["value_pattern_match"].coverage == 0
        assert "PATTERN_EVIDENCE_INCOMPLETE" in explanation.blockers
    if missing == "pair_limit":
        assert "CONTEXT_EVIDENCE_INCOMPLETE" in explanation.blockers


@pytest.mark.parametrize("mode", ["exact", "estimated", "nullable", "no_constraint"])
async def test_identity_hint_requires_exact_unique_nonnull_db_key(mode: str) -> None:
    data = await NormalizedDataProfiler(
        NormalizedProfilingOptions(distinct_k=16 if mode == "estimated" else 1024)
    ).profile(
        normalized_stream([{"customer_key": IntegerScalar(value=i)} for i in range(30)])
    )
    target = column("customer_key", "integer").model_copy(
        update={"nullable": mode == "nullable"}
    )
    t = table("customers", target).model_copy(
        update={
            "unique_constraints": ()
            if mode == "no_constraint"
            else (("customer_key",),)
        }
    )
    db = catalog(t)
    semantic = DatabaseSemanticCatalog(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        database_fingerprint=db.database_fingerprint,
        tables=(
            SemanticTable(
                schema_name="public",
                table_name="customers",
                identity_keys=(("customer_key",),),
            ),
        ),
    )
    result = await DeterministicMapper().rank(
        data, db, scope=scope_for(db), semantic_catalog=semantic
    )
    evidence = result.fields[0].explanations[0].identity_evidence
    assert "IDENTITY_KEY_DECLARED" in evidence
    assert ("IDENTITY_KEY_SUPPORTED" in evidence) == (mode == "exact")
    assert data.fields[0].unique_mode == (
        "estimated" if mode == "estimated" else "exact"
    )


async def test_leading_zero_identifier_and_explicit_null_are_not_coerced() -> None:
    field = (await profile({"account_code": StringScalar(value="000123")})).fields[0]
    assert (
        type_compatibility(field, column("account_code", "integer")).status
        == "incompatible"
    )
    assert type_compatibility(field, column("account_code")).status == "compatible"
    null = (await profile({"email": NullScalar()})).fields[0]
    target = column("email")
    assert target.inspection is not None
    target = target.model_copy(
        update={
            "nullable": False,
            "inspection": target.inspection.model_copy(
                update={"default": "'fallback'"}
            ),
        }
    )
    evidence = type_compatibility(null, target)
    assert "REQUIRED_VALUE_MISSING" in evidence.blockers


async def test_category_and_identity_patterns_use_distinct_profile_flags() -> None:
    category = (await profile({"segment": StringScalar(value="retail")})).fields[0]
    identifiers = await NormalizedDataProfiler().profile(
        normalized_stream([{"account_id": IntegerScalar(value=i)} for i in range(30)])
    )
    identity = identifiers.fields[0]
    assert category.categorical
    assert not identity.categorical
    assert pattern_match(category, ("categorical",)).score == 1
    assert pattern_match(identity, ("categorical",)).score == 0
    assert pattern_match(identity, ("identity",)).score == 1
    assert pattern_match(category, ("identity",)).score == 0
