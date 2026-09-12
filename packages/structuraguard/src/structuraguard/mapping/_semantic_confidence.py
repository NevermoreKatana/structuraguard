"""SDK aggregation: self-score не снимает blockers и не скрывает конкурентов."""

from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Context, Decimal, localcontext

from structuraguard.contracts.semantic_mapping import (
    SemanticAction,
    SemanticAssessment,
    SemanticCandidateScore,
    SemanticMappingCandidateSet,
    SemanticMappingDecision,
    SemanticMappingOptions,
    SemanticScoredChoice,
)


def aggregate(
    group: SemanticMappingCandidateSet,
    decision: SemanticMappingDecision,
    options: SemanticMappingOptions,
    *,
    security_warning: bool = False,
) -> tuple[tuple[SemanticScoredChoice, ...], Decimal, bool, tuple[str, ...]]:
    """Вернуть полные scores, минимум required choices, ambiguity и review reasons."""
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return _aggregate(group, decision, options, security_warning=security_warning)


def proposal_action(
    actions: tuple[SemanticAction, ...], reasons: tuple[str, ...]
) -> SemanticAction:
    """Любой reject сохраняется; blockers не разрешают auto при высоком score."""
    if "reject" in actions:
        return "reject"
    if not actions or reasons or "confirm" in actions:
        return "confirm"
    return "auto"


def _choice_action(
    scores: tuple[SemanticCandidateScore, ...],
    selected: tuple[str, ...],
    options: SemanticMappingOptions,
    *,
    ambiguous: bool,
    required: bool,
    blocked: bool,
) -> SemanticAction:
    if not selected:
        return "confirm" if ambiguous else "reject" if required else "auto"
    minimum = min(s.score for s in scores if s.candidate_id in selected)
    if minimum < options.review_threshold:
        return "reject"
    if ambiguous or blocked or minimum < options.auto_threshold:
        return "confirm"
    return "auto"


def _aggregate(
    group: SemanticMappingCandidateSet,
    decision: SemanticMappingDecision,
    options: SemanticMappingOptions,
    *,
    security_warning: bool,
) -> tuple[tuple[SemanticScoredChoice, ...], Decimal, bool, tuple[str, ...]]:
    unit = Decimal("0.000001")
    bases = {t.candidate_id: t.base_score for t in group.tables}
    bases.update({c.candidate_id: c.explanation.base_score for c in group.columns})
    bases.update({r.candidate_id: r.base_score for r in group.relations})
    field_index = {f.source_id: f for f in group.fields}
    columns = {c.candidate_id: c for c in group.columns}
    # M9 ambiguity и collision пересчитаны по проверенному полному assignment.
    blockers = {
        c.candidate_id: set(c.explanation.blockers)
        - {"AMBIGUOUS_TARGET", "TARGET_COLLISION"}
        for c in group.columns
    }
    blockers.update(
        {
            r.candidate_id: {"FK_STRATEGY_REQUIRED"} if r.requires_strategy else set()
            for r in group.relations
        }
    )
    validation = {
        key: options.validation_penalty if values - {"CONFUSABLE_NAME"} else Decimal(0)
        for key, values in blockers.items()
    }
    security = {
        key: max(
            columns[key].explanation.security_penalty if key in columns else Decimal(0),
            options.security_penalty
            if security_warning
            or "CLASSIFICATION_INCOMPLETE" in group.reasons
            or "CONFUSABLE_NAME" in blockers.get(key, set())
            else Decimal(0),
        )
        for key in bases
    }
    active_tables = {v for t in decision.tables for v in t.selected_candidate_ids}
    model_reasons = {choice.source_id: choice.reason_code for choice in decision.tables}
    model_reasons.update(
        (choice.source_id, choice.reason_code)
        for choice in (*decision.columns, *decision.relations)
    )
    entries: list[
        tuple[str, str, tuple[str, ...], tuple[SemanticAssessment, ...], bool]
    ] = [
        (t.source_id, t.status, t.selected_candidate_ids, t.assessments, True)
        for t in decision.tables
    ]
    entries.extend(
        (
            c.source_id,
            c.status,
            (c.selected_candidate_id,) if c.selected_candidate_id else (),
            c.assessments,
            True,
        )
        for c in decision.columns
    )
    for relation in decision.relations:
        candidates = [r for r in group.relations if r.source_id == relation.source_id]
        required = not candidates or any(
            r.parent_table_candidate_id in active_tables
            and r.child_table_candidate_id in active_tables
            for r in candidates
        )
        entries.append(
            (
                relation.source_id,
                relation.status,
                (relation.selected_candidate_id,)
                if relation.selected_candidate_id
                else (),
                relation.assessments,
                required,
            )
        )
    choices = []
    selected_scores = []
    all_reasons = set(group.reasons)
    if decision.review_required:
        all_reasons.add("MODEL_REQUESTED_REVIEW")
    if security_warning:
        all_reasons.add("SECURITY_REVIEW_REQUIRED")
    for source_id, status, selected, assessments, required in entries:
        reasons: set[str] = set()
        reported_reasons = {model_reasons[source_id]} | {
            a.reason_code for a in assessments
        }
        selected_reasons = {model_reasons[source_id]} | {
            a.reason_code for a in assessments if a.candidate_id in selected
        }
        # Число не отменяет явную неопределённость, включая неоценённого конкурента.
        if "INSUFFICIENT_EVIDENCE" in reported_reasons or (
            selected and "NO_MATCH" in selected_reasons
        ):
            reasons.add("MODEL_REQUESTED_REVIEW")
        mixed = {
            a.candidate_id: (
                (1 - options.llm_weight) * bases[a.candidate_id]
                + options.llm_weight * Decimal(a.semantic_score)
            ).quantize(unit)
            for a in assessments
        }
        # Сравнение альтернатив учитывает SDK penalties, а не только self-score.
        raw = {
            key: max(
                Decimal(0), value - validation.get(key, Decimal(0)) - security[key]
            )
            for key, value in mixed.items()
        }
        ordered = sorted(raw, key=lambda k: (-raw[k], k))
        unselected = [k for k in ordered if k not in selected]
        ambiguous = (
            status == "ambiguous" or "MULTIPLE_PLAUSIBLE_TARGETS" in reported_reasons
        )
        if selected and unselected:
            gap = min(raw[k] for k in selected) - max(raw[k] for k in unselected)
            ambiguous |= gap < options.ambiguity_margin
            if gap < 0:
                reasons.add("MODEL_SDK_DISAGREEMENT")
        elif not selected and len(ordered) > 1:
            ambiguous |= raw[ordered[0]] - raw[ordered[1]] < options.ambiguity_margin
        if source_id in field_index:
            field = field_index[source_id].ranked
            if field.competitor_count > len(field.candidates) and selected:
                cutoff = field.explanations[-1].base_score
                bound = min(
                    Decimal(1),
                    (1 - options.llm_weight) * min(Decimal(1), cutoff + unit)
                    + options.llm_weight,
                ).quantize(unit, rounding=ROUND_CEILING)
                if min(raw[k] for k in selected) - bound < options.ambiguity_margin:
                    reasons.add("UNASSESSED_COMPETITOR")
                    ambiguous = True
            for key in selected:
                reasons.update(
                    b
                    for b in columns[key].explanation.blockers
                    if b not in {"AMBIGUOUS_TARGET", "TARGET_COLLISION"}
                )
        if ambiguous:
            reasons.add("AMBIGUOUS_TARGET")
        penalty = options.ambiguity_penalty if ambiguous else Decimal(0)
        scores = tuple(
            SemanticCandidateScore(
                candidate_id=a.candidate_id,
                deterministic_score=bases[a.candidate_id],
                llm_score=Decimal(a.semantic_score),
                llm_weight=options.llm_weight,
                ambiguity_penalty=penalty,
                validation_penalty=validation.get(a.candidate_id, Decimal(0)),
                security_penalty=security[a.candidate_id],
                score=max(Decimal(0), raw[a.candidate_id] - penalty).quantize(unit),
            )
            for a in assessments
        )
        if required:
            if not selected:
                reasons.add("UNMAPPED_REQUIRED_SOURCE")
                selected_scores.append(Decimal(0))
            for score in scores:
                if score.candidate_id in selected:
                    selected_scores.append(score.score)
                    if score.score < options.auto_threshold:
                        reasons.add(
                            "SCORE_REQUIRES_REVIEW"
                            if score.score >= options.review_threshold
                            else "SCORE_REJECTED"
                        )
        for relation_candidate in group.relations:
            if (
                relation_candidate.candidate_id in selected
                and relation_candidate.requires_strategy
            ):
                reasons.add("FK_STRATEGY_REQUIRED")
        if any(security[k] for k in selected):
            reasons.add("SECURITY_REVIEW_REQUIRED")
        choices.append(
            SemanticScoredChoice(
                source_id=source_id,
                selected_candidate_ids=selected,
                scores=scores,
                ambiguous=ambiguous,
                reasons=tuple(sorted(reasons)),
                action=_choice_action(
                    scores,
                    selected,
                    options,
                    ambiguous=ambiguous,
                    required=required,
                    blocked=bool(
                        reasons
                        or group.reasons
                        or decision.review_required
                        or security_warning
                    ),
                ),
            )
        )
        all_reasons.update(reasons)
    return (
        tuple(choices),
        min(selected_scores, default=Decimal(0)),
        any(c.ambiguous for c in choices),
        tuple(sorted(all_reasons)),
    )
