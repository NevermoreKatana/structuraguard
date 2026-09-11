"""Чистая сборка SDK scores, конфликтов, explanations и bindings."""

import unicodedata
from collections import Counter
from decimal import Decimal

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import ProducerMetadata
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    CandidateExplanation,
    DeterministicMappingOptions,
    DeterministicMappingResult,
    FieldCandidates,
    MappingScope,
)
from structuraguard.contracts.mapping import MappingCandidate
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog

from ._inputs import Budget, bounded_size, failure
from ._ranking import Source, TopCandidates
from ._scores import candidate_status, difference, final_score


def build_result(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    scope: MappingScope,
    semantic: DatabaseSemanticCatalog,
    options: DeterministicMappingOptions,
    retained: list[tuple[Source, TopCandidates, set[str]]],
    budget: Budget,
) -> DeterministicMappingResult:
    collisions = Counter(
        top.ordered()[0].target.ref for _, top, _ in retained if top.heap
    )
    options_hash = canonical_sha256_value(
        {
            "options": options.canonical_json(),
            "unicode": unicodedata.unidata_version,
        }
    )
    producer = ProducerMetadata(
        component_id="mapping.deterministic",
        component_version="1.0.0",
        sdk_version="0.3.0",
    )
    bindings = {
        "source": profile.source.source_fingerprint,
        "extraction": profile.extraction_fingerprint,
        "parse_plan": profile.parse_plan_fingerprint,
        "manifest": profile.normalized_manifest_fingerprint,
        "content": profile.normalized_data_fingerprint,
        "profile": profile.profile_fingerprint,
        "database": catalog.database_fingerprint,
        "target": catalog.target_id,
        "target_policy": catalog.target_policy_fingerprint,
        "scope": scope.fingerprint,
        "options": options_hash,
        "semantic": semantic.fingerprint,
        "producer": producer.canonical_json(),
    }
    results = []
    result_bytes = 0
    for source, top, reasons in retained:
        ordered = top.ordered()
        gap = (
            difference(ordered[0].score, ordered[1].score) if len(ordered) > 1 else None
        )
        ambiguous = gap is not None and gap < options.ambiguity_margin
        collision = bool(ordered) and collisions[ordered[0].target.ref] > 1
        if ambiguous:
            reasons.add("AMBIGUOUS_TARGET")
        if collision:
            reasons.add("TARGET_COLLISION")
        ambiguity_penalty = options.ambiguity_penalty if ambiguous else Decimal(0)
        validation_penalty = options.collision_penalty if collision else Decimal(0)
        candidates = []
        explanations = []
        for ranked in ordered[: options.top_k]:
            candidate_id = "candidate:" + canonical_sha256_value(
                {
                    "algorithm": options.algorithm,
                    "bindings": bindings,
                    "source": source.profile.field.canonical_json(),
                    "target": ranked.target.ref.canonical_json(),
                }
            ).removeprefix("sha256:")
            score = final_score(ranked.score, ambiguity_penalty, validation_penalty)
            candidate = MappingCandidate(
                candidate_id=candidate_id,
                source_fingerprint=profile.source.source_fingerprint,
                extraction_fingerprint=profile.extraction_fingerprint,
                parse_plan_fingerprint=profile.parse_plan_fingerprint,
                normalized_fingerprint=profile.normalized_manifest_fingerprint,
                database_fingerprint=catalog.database_fingerprint,
                target_id=catalog.target_id,
                target_policy_fingerprint=catalog.target_policy_fingerprint,
                producer=producer,
                source=source.profile.field,
                target=ranked.target.ref,
                confidence=score,
                evidence=tuple(s.code for s in ranked.signals if s.value),
            )
            blockers = set(ranked.blockers)
            if ambiguous:
                blockers.add("AMBIGUOUS_TARGET")
            if collision:
                blockers.add("TARGET_COLLISION")
            candidates.append(candidate)
            explanations.append(
                CandidateExplanation(
                    candidate_id=candidate_id,
                    base_score=ranked.score,
                    final_score=score,
                    signals=ranked.signals,
                    compatibility=ranked.evidence.compatibility.status,
                    ambiguity_penalty=ambiguity_penalty,
                    validation_penalty=validation_penalty,
                    blockers=tuple(sorted(blockers)),
                    foreign_key_ids=ranked.foreign_key_ids,
                    identity_evidence=ranked.identity_evidence,
                )
            )
        status: str = "unmapped"
        if candidates:
            status = candidate_status(
                candidates[0].confidence,
                explanations[0].compatibility,
                explanations[0].blockers,
                options,
            )
        else:
            reasons.add("NO_ADMISSIBLE_CANDIDATES")
        field_result = FieldCandidates.model_validate(
            {
                "source": source.profile.field,
                "candidates": tuple(candidates),
                "explanations": tuple(explanations),
                "status": status,
                "ambiguous": ambiguous,
                "gap": gap,
                "competitor_count": top.count,
                "tie_count": top.tie_count,
                "reasons": tuple(sorted(reasons)),
            }
        )
        result_bytes += bounded_size(field_result, options.max_result_bytes)
        if result_bytes > options.max_result_bytes:
            raise failure("MAPPING_LIMIT_EXCEEDED", "result_bytes")
        results.append(field_result)
    budget.retain(result_bytes)
    result = DeterministicMappingResult(
        unicode_version=unicodedata.unidata_version,
        profile_fingerprint=profile.profile_fingerprint,
        normalized_data_fingerprint=profile.normalized_data_fingerprint,
        options_fingerprint=options_hash,
        scope_fingerprint=scope.fingerprint,
        semantic_catalog_fingerprint=semantic.fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        classification=profile.classification,
        fields=tuple(results),
    )
    bounded_size(result, options.max_result_bytes)
    return result
