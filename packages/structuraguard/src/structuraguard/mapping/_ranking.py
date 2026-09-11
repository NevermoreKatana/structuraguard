"""Чистые сигналы, SDK score и bounded top-k; никакого I/O."""

import heapq
from dataclasses import dataclass, field
from decimal import Decimal

from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    TableCatalog,
)
from structuraguard.contracts.deterministic_mapping import (
    CandidateSignal,
    MappingWeights,
)
from structuraguard.contracts.profiling import NormalizedFieldProfile
from structuraguard.contracts.semantic_catalog import SemanticTable

from ._aliases import alias_score
from ._compatibility import (
    Compatibility,
    PatternMatch,
    pattern_match,
    type_compatibility,
)
from ._names import Name, NameEvidence, best_names
from ._scores import product, total


@dataclass(frozen=True, repr=False)
class Source:
    profile: NormalizedFieldProfile
    names: tuple[Name, ...]
    context: tuple[Name, ...]


@dataclass(frozen=True, repr=False)
class Target:
    table: TableCatalog
    column: ColumnCatalog
    ref: CatalogColumnRef
    name: Name
    aliases: tuple[Name, ...]
    table_names: tuple[Name, ...]
    patterns: tuple[str, ...]
    semantic_table: SemanticTable | None = None

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.table.schema_name,
            self.table.name,
            self.column.name,
            self.table.table_id,
            self.column.column_id,
        )


@dataclass(frozen=True)
class BaseEvidence:
    names: NameEvidence
    alias: Decimal
    compatibility: Compatibility
    pattern: PatternMatch


def base_evidence(source: Source, target: Target, dialect: str) -> BaseEvidence:
    return BaseEvidence(
        best_names(source.names, target.name),
        alias_score(source.names, target.name, target.aliases),
        type_compatibility(source.profile, target.column, dialect=dialect),
        pattern_match(source.profile, target.patterns),
    )


def score_signals(
    evidence: BaseEvidence,
    weights: MappingWeights,
    *,
    structure: Decimal,
    graph: Decimal,
    structure_available: bool,
    graph_available: bool,
) -> tuple[CandidateSignal, ...]:
    """Опубликовать exact/normalized отдельно; только aggregate name имеет вес."""
    items = (
        CandidateSignal(
            code="exact_name",
            value=evidence.names.exact,
            weight=Decimal(0),
            contribution=Decimal(0),
        ),
        CandidateSignal(
            code="normalized_name",
            value=max(evidence.names.normalized, evidence.names.compact),
            weight=Decimal(0),
            contribution=Decimal(0),
        ),
        CandidateSignal(
            code="transliterated_name",
            value=evidence.names.transliterated,
            weight=Decimal(0),
            contribution=Decimal(0),
        ),
        CandidateSignal(
            code="name_similarity",
            value=evidence.names.score,
            weight=weights.name_similarity,
            contribution=product(evidence.names.score, weights.name_similarity),
        ),
        CandidateSignal(
            code="alias_match",
            value=evidence.alias,
            weight=weights.alias_match,
            contribution=product(evidence.alias, weights.alias_match),
        ),
        CandidateSignal(
            code="type_compatibility",
            value=evidence.compatibility.score,
            weight=weights.type_compatibility,
            contribution=product(
                evidence.compatibility.score, weights.type_compatibility
            ),
            available=evidence.compatibility.status != "unknown",
            reasons=evidence.compatibility.blockers,
        ),
        CandidateSignal(
            code="value_pattern_match",
            value=evidence.pattern.score,
            weight=weights.value_pattern_match,
            contribution=product(evidence.pattern.score, weights.value_pattern_match),
            available=evidence.pattern.available,
            coverage=evidence.pattern.coverage,
            reasons=evidence.pattern.blockers,
        ),
        CandidateSignal(
            code="structural_context",
            value=structure,
            weight=weights.structural_context,
            contribution=product(structure, weights.structural_context),
            available=structure_available,
        ),
        CandidateSignal(
            code="database_relation_score",
            value=graph,
            weight=weights.database_relation_score,
            contribution=product(graph, weights.database_relation_score),
            available=graph_available,
        ),
    )
    return items


@dataclass(frozen=True, repr=False)
class Ranked:
    target: Target
    evidence: BaseEvidence
    signals: tuple[CandidateSignal, ...]
    blockers: tuple[str, ...]
    foreign_key_ids: tuple[str, ...] = ()
    identity_evidence: tuple[str, ...] = ()

    @property
    def score(self) -> Decimal:
        return total(s.contribution for s in self.signals)

    @property
    def key(self) -> tuple[Decimal, tuple[str, str, str, str, str]]:
        return self.score.copy_negate(), self.target.key

    def __lt__(self, other: "Ranked") -> bool:
        # Root heap — худший retained target; canonical sort key направлен обратно.
        return self.key > other.key


@dataclass
class TopCandidates:
    """Heap хранит runner-up даже при пользовательском k=1."""

    capacity: int
    heap: list[Ranked] = field(default_factory=list)
    count: int = 0
    tie_count: int = 0
    best_score: Decimal = Decimal("-1")

    def add(self, value: Ranked) -> None:
        self.count += 1
        if value.score > self.best_score:
            self.best_score, self.tie_count = value.score, 1
        elif value.score == self.best_score:
            self.tie_count += 1
        if len(self.heap) < self.capacity:
            heapq.heappush(self.heap, value)
        elif value.key < self.heap[0].key:
            heapq.heapreplace(self.heap, value)

    def ordered(self) -> tuple[Ranked, ...]:
        return tuple(sorted(self.heap, key=lambda value: value.key))
