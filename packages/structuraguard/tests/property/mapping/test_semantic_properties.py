"""Перестановки metadata и assessments не меняют semantic выбор и SDK score."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import decision_for, mapping_options

from structuraguard.contracts.semantic_mapping import SemanticMappingOptions
from structuraguard.mapping import prepare_semantic_mapping
from structuraguard.mapping._semantic_confidence import aggregate
from structuraguard.mapping._semantic_validation import validate_decision


@settings(max_examples=12, deadline=None)
@given(st.permutations(("customers", "suppliers", "partners")), st.integers(1, 3))
def test_semantic_candidates_and_scores_are_stable_under_permutations(
    order: list[str], k: int
) -> None:
    async def run() -> None:
        data = await profile()
        db = catalog(*(table(name, column("email")) for name in order))
        canonical = catalog(*(table(name, column("email")) for name in sorted(order)))
        options = SemanticMappingOptions(top_k=k)
        actual = await prepare_semantic_mapping(
            data,
            db,
            scope=scope_for(db),
            options=options,
            ranking_options=mapping_options(),
        )
        expected = await prepare_semantic_mapping(
            data,
            canonical,
            scope=scope_for(canonical),
            options=options,
            ranking_options=mapping_options(),
        )
        assert actual == expected
        group = actual.groups[0]
        assert len(group.columns) == len(group.tables) == k
        decision = decision_for(group)
        permuted = decision.model_copy(
            update={
                "tables": tuple(
                    t.model_copy(update={"assessments": tuple(reversed(t.assessments))})
                    for t in reversed(decision.tables)
                ),
                "columns": tuple(
                    c.model_copy(update={"assessments": tuple(reversed(c.assessments))})
                    for c in reversed(decision.columns)
                ),
            }
        )
        validated = validate_decision(permuted.canonical_json(), group, options)
        first = aggregate(group, decision, options)
        second = aggregate(group, validated, options)
        assert first[1:] == second[1:]
        for left, right in zip(first[0], second[0], strict=True):
            assert left.model_copy(
                update={
                    "scores": tuple(sorted(left.scores, key=lambda s: s.candidate_id))
                }
            ) == right.model_copy(
                update={
                    "scores": tuple(sorted(right.scores, key=lambda s: s.candidate_id))
                }
            )

    asyncio.run(run())
