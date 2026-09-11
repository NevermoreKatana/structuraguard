"""Новые contracts не меняют legacy MappingCandidate и связывают explanations."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from structuraguard.contracts import DeterministicMappingOptions, MappingWeights
from structuraguard.contracts.database import CatalogColumnRef
from structuraguard.contracts.deterministic_mapping import MappingScope
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)


def test_options_validate_thresholds_and_hard_ceilings() -> None:
    for update in (
        {"top_k": 0},
        {"top_k": 11},
        {"max_pairs": 2000001},
        {"review_threshold": Decimal("0.95")},
        {"ambiguity_margin": Decimal(0)},
        {"llm_weight": Decimal(0)},
    ):
        with pytest.raises(ValidationError):
            DeterministicMappingOptions.model_validate(update)
    options = DeterministicMappingOptions(
        weights=MappingWeights(
            name_similarity=Decimal(1),
            alias_match=Decimal(0),
            type_compatibility=Decimal(0),
            value_pattern_match=Decimal(0),
            structural_context=Decimal(0),
            database_relation_score=Decimal(0),
        )
    )
    assert (
        DeterministicMappingOptions.model_validate_json(options.model_dump_json())
        == options
    )


def test_scope_and_semantic_aliases_are_canonical_and_immutable() -> None:
    a = CatalogColumnRef(table_id="a", column_id="email")
    b = CatalogColumnRef(table_id="b", column_id="email")
    first = MappingScope(
        target_id="test",
        target_policy_fingerprint="sha256:" + "a" * 64,
        allow=(b, a, a),
    )
    second = first.model_copy(update={"allow": (a, b)})
    assert first.fingerprint == second.fingerprint
    with pytest.raises(ValidationError):
        first.allow = ()
    entry = SemanticTable(
        schema_name="public",
        table_name="customers",
        columns=(
            SemanticColumn(column_name="inn", aliases=("Tax ID", "ИНН", "Tax ID")),
        ),
    )
    assert entry.columns[0].aliases == ("Tax ID", "ИНН")
    semantic = DatabaseSemanticCatalog(
        target_id="test",
        target_policy_fingerprint="sha256:" + "a" * 64,
        database_fingerprint="sha256:" + "b" * 64,
        tables=(entry, entry),
    )
    assert semantic.tables == (entry,)
    with pytest.raises(ValidationError):
        SemanticTable(
            schema_name="public",
            table_name="customers",
            columns=(
                SemanticColumn(column_name="inn", semantic_type="inn"),
                SemanticColumn(column_name="inn", semantic_type="email"),
            ),
        )
