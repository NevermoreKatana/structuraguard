"""Сборка bounded evidence и кандидатов без выбора победителя."""

from collections import Counter
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.parsing import (
    StructureCandidate,
    StructureEvidence,
)
from structuraguard.contracts.source import ExtractedDatasetManifest
from structuraguard.contracts.structure import (
    ProfileCount,
    ProfilerObservation,
    StructuralProfilingOptions,
)
from structuraguard.structure._samples import Samples


def counts(counter: Counter[str]) -> tuple[ProfileCount, ...]:
    return tuple(
        ProfileCount(name=name, count=count)
        for name, count in sorted(counter.items())
        if count
    )


def ratio(numerator: int, denominator: int) -> Decimal:
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        return (Decimal(numerator) / max(denominator, 1)).quantize(Decimal("0.0001"))


def field_name(label: str, fallback: str) -> str:
    cleaned = "".join(
        char.lower() if char.isascii() and char.isalnum() else "_" for char in label
    ).strip("_")[:64]
    if not cleaned:
        return fallback
    return "field_" + cleaned if cleaned[0].isdigit() else cleaned


class Observations:
    def __init__(
        self,
        samples: Samples,
        manifest: ExtractedDatasetManifest,
        options: StructuralProfilingOptions,
    ) -> None:
        self.samples = samples
        self.manifest = manifest
        self.options = options
        self.evidence: list[StructureEvidence] = []
        self.candidates: list[StructureCandidate] = []
        self.identifiers: set[str] = set()
        self.size = 0

    def add(
        self,
        observation: ProfilerObservation,
        confidence: Decimal,
        *,
        candidate: ParsePlanKind | None = None,
    ) -> None:
        reference_tuple = observation.source_refs
        code = observation.kind
        # Новые observations все имеют общий bounded source_refs contract.
        digest = canonical_sha256_value(observation).removeprefix("sha256:")
        identifier = "evidence_" + digest[:24]
        if identifier in self.identifiers:
            return
        if len(self.evidence) >= self.options.max_observations:
            self.samples.reasons.add("observation_limit")
            return
        item = StructureEvidence(
            evidence_id=identifier,
            code=code,
            source_refs=reference_tuple,
            observation=observation,
            confidence=confidence,
        )
        size = len(item.canonical_json().encode("utf-8"))
        if self.size + size > self.options.max_profile_bytes:
            self.samples.reasons.add("profile_bytes")
            return
        self.evidence.append(item)
        self.identifiers.add(identifier)
        self.size += size
        if candidate is not None:
            self.candidates.append(
                StructureCandidate(
                    candidate_id="candidate_" + digest[:24],
                    source=self.manifest.source,
                    extraction_fingerprint=self.manifest.extraction_fingerprint,
                    plan_kind=candidate,
                    confidence=confidence,
                    evidence=reference_tuple,
                    rationale_codes=(code,),
                    observation_ids=(identifier,),
                )
            )

    def ranked(self) -> tuple[StructureCandidate, ...]:
        ordered = sorted(
            self.candidates,
            key=lambda item: (
                item.confidence.copy_negate(),
                item.plan_kind,
                item.candidate_id,
            ),
        )
        if len(ordered) > self.options.max_candidates:
            self.samples.reasons.add("candidate_limit")
        return tuple(ordered[: self.options.max_candidates])
