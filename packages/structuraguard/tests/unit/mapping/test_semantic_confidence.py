"""Ambiguity и недостающие competitors не скрываются высоким LLM score."""

from decimal import Decimal, localcontext

import pytest
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import decision_for, mapping_options, run_mapper

from structuraguard.contracts.common import PipelineStatus
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.semantic_mapping import SemanticMappingOptions
from structuraguard.mapping import prepare_semantic_mapping
from structuraguard.mapping._semantic_confidence import aggregate

pytestmark = pytest.mark.anyio


async def test_equal_semantic_scores_preserve_explicit_ambiguity() -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0])
    decision = decision.model_copy(
        update={
            "columns": tuple(
                c.model_copy(
                    update={
                        "assessments": tuple(
                            a.model_copy(update={"semantic_score": "1.000000"})
                            for a in c.assessments
                        )
                    }
                )
                for c in decision.columns
            )
        }
    )
    result, _, _ = await run_mapper(data, db, decision)
    assert result.groups[0].ambiguous
    assert result.status is PipelineStatus.NEEDS_REVIEW
    assert "AMBIGUOUS_TARGET" in result.groups[0].reasons


async def test_hidden_top_one_competitor_cannot_be_confirmed_by_self_score() -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    ranked = mapping_options().model_copy(update={"top_k": 1})
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=ranked
    )
    _, _, ambiguous, reasons = aggregate(
        prepared.groups[0], decision_for(prepared.groups[0]), SemanticMappingOptions()
    )
    assert ambiguous and "UNASSESSED_COMPETITOR" in reasons


async def test_llm_is_only_one_signal_and_decimal_context_is_local() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=DeterministicMappingOptions()
    )
    decision = decision_for(prepared.groups[0])
    first = aggregate(prepared.groups[0], decision, SemanticMappingOptions())
    with localcontext() as ctx:
        ctx.prec = 2
        second = aggregate(prepared.groups[0], decision, SemanticMappingOptions())
    assert first == second
    score = first[0][1].scores[0]
    signals = prepared.groups[0].columns[0].explanation.signals
    assert {s.code for s in signals if s.weight} == {
        "name_similarity",
        "alias_match",
        "type_compatibility",
        "value_pattern_match",
        "structural_context",
        "database_relation_score",
    }
    assert score.deterministic_score == sum(
        (s.contribution for s in signals), Decimal(0)
    ).quantize(Decimal("0.000001"))
    assert score.score < score.llm_score
    assert score.score == (
        Decimal("0.8") * score.deterministic_score + Decimal("0.2") * score.llm_score
    ).quantize(Decimal("0.000001"))


async def test_model_review_cannot_be_overridden_by_high_score() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0]).model_copy(
        update={"review_required": True}
    )
    result, _, _ = await run_mapper(data, db, decision)
    assert result.status is PipelineStatus.NEEDS_REVIEW
    assert "MODEL_REQUESTED_REVIEW" in result.groups[0].reasons


@pytest.mark.parametrize(
    ("base", "action"),
    [
        ("0.900000", "auto"),
        ("0.899999", "confirm"),
        ("0.700000", "confirm"),
        ("0.699999", "reject"),
    ],
)
async def test_sdk_threshold_boundaries(base: str, action: str) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    candidate = group.columns[0]
    group = group.model_copy(
        update={
            "columns": (
                candidate.model_copy(
                    update={
                        "explanation": candidate.explanation.model_copy(
                            update={"base_score": Decimal(base)}
                        )
                    }
                ),
            )
        }
    )
    choices, _, _, _ = aggregate(
        group, decision_for(group), SemanticMappingOptions(llm_weight=Decimal(0))
    )
    assert (
        next(c for c in choices if c.source_id == candidate.source_id).action == action
    )


async def test_validation_and_security_penalties_are_sdk_evidence() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    candidate = group.columns[0]
    group = group.model_copy(
        update={
            "columns": (
                candidate.model_copy(
                    update={
                        "explanation": candidate.explanation.model_copy(
                            update={
                                "blockers": (
                                    "DOMAIN_VALIDATION_REQUIRED",
                                    "CONFUSABLE_NAME",
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    choices, confidence, _, reasons = aggregate(
        group, decision_for(group), SemanticMappingOptions()
    )
    choice = next(c for c in choices if c.source_id == candidate.source_id)
    score = choice.scores[0]
    assert score.validation_penalty == Decimal("0.10")
    assert score.security_penalty == Decimal("0.20")
    assert score.score == confidence == Decimal("0.698000")
    assert choice.action == "reject"
    assert "CONFUSABLE_NAME" in reasons


async def test_ambiguity_is_confirm_even_without_selected_target() -> None:
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
                    update={"status": "ambiguous", "selected_candidate_id": None}
                ),
            )
        }
    )
    choices, _, ambiguous, _ = aggregate(
        prepared.groups[0], decision, SemanticMappingOptions()
    )
    assert ambiguous and choices[1].action == "confirm"


@pytest.mark.parametrize("penalty", ["0", "1"])
async def test_zero_penalty_cannot_remove_blocker_and_large_penalties_clamp(
    penalty: str,
) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    candidate = group.columns[0]
    group = group.model_copy(
        update={
            "columns": (
                candidate.model_copy(
                    update={
                        "explanation": candidate.explanation.model_copy(
                            update={"blockers": ("DOMAIN_VALIDATION_REQUIRED",)}
                        )
                    }
                ),
            )
        }
    )
    choices, confidence, _, _ = aggregate(
        group,
        decision_for(group),
        SemanticMappingOptions(
            validation_penalty=Decimal(penalty), security_penalty=Decimal(penalty)
        ),
        security_warning=True,
    )
    assert choices[1].action == ("confirm" if penalty == "0" else "reject")
    assert confidence == (Decimal("0.998000") if penalty == "0" else Decimal(0))
