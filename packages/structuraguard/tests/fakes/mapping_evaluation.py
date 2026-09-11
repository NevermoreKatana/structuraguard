"""Offline evaluation на синтетических случаях; не часть runtime SDK."""

from collections import defaultdict
from decimal import Context, Decimal, localcontext
from typing import Literal

from pydantic import TypeAdapter

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
    MappingWeights,
)
from structuraguard.contracts.semantic_catalog import (
    DatabaseSemanticCatalog,
    SemanticColumn,
    SemanticTable,
)
from structuraguard.mapping import DeterministicMapper
from tests.fakes.mapping import catalog, column, profile, refs, table


class EvaluationTarget(FrozenContract):
    schema_name: str
    table_name: str
    column_name: str
    kind: str = "text"
    aliases: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.schema_name}.{self.table_name}.{self.column_name}"


class EvaluationCase(FrozenContract):
    case_id: str
    split: Literal["calibration", "holdout"]
    language: Literal["ru", "en"]
    group: Literal["names", "aliases", "patterns", "generic", "transliteration"]
    source_name: str
    entity_type: str = "row"
    value: str = "sample"
    targets: tuple[EvaluationTarget, ...]
    expected: str | None


class Observation(FrozenContract):
    case_id: str
    language: str
    group: str
    rank: int | None
    positive: bool
    auto: bool
    correct: bool
    review: bool


class EvaluationMetrics(FrozenContract):
    cases: int
    positive_cases: int
    recall_at_1: str
    recall_at_5: str
    recall_at_10: str
    mrr: str
    false_auto: int
    auto_precision: str | None
    auto_coverage: str
    review_coverage: str
    abstain_coverage: str


def load_cases(data: str) -> tuple[EvaluationCase, ...]:
    return TypeAdapter(tuple[EvaluationCase, ...]).validate_json(data)


def weight_grid() -> tuple[MappingWeights, ...]:
    return (
        MappingWeights(),
        MappingWeights(
            name_similarity=Decimal("0.60"),
            alias_match=Decimal("0.10"),
            type_compatibility=Decimal("0.20"),
            value_pattern_match=Decimal("0.05"),
            structural_context=Decimal("0.025"),
            database_relation_score=Decimal("0.025"),
        ),
        MappingWeights(
            name_similarity=Decimal("0.25"),
            alias_match=Decimal("0.30"),
            type_compatibility=Decimal("0.20"),
            value_pattern_match=Decimal("0.10"),
            structural_context=Decimal("0.10"),
            database_relation_score=Decimal("0.05"),
        ),
    )


async def evaluate(
    cases: tuple[EvaluationCase, ...], weights: MappingWeights
) -> tuple[Observation, ...]:
    result = []
    for case in sorted(cases, key=lambda c: c.case_id):
        data = await profile(
            {case.source_name: StringScalar(value=case.value)}, entity=case.entity_type
        )
        db = catalog(
            *(
                table(t.table_name, column(t.column_name, t.kind), schema=t.schema_name)
                for t in case.targets
            )
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
            tables=tuple(
                SemanticTable(
                    schema_name=t.schema_name,
                    table_name=t.table_name,
                    columns=(
                        SemanticColumn(column_name=t.column_name, aliases=t.aliases),
                    ),
                )
                for t in case.targets
            ),
        )
        field = (
            await DeterministicMapper(
                DeterministicMappingOptions(top_k=10, weights=weights)
            ).rank(data, db, scope=scope, semantic_catalog=semantic)
        ).fields[0]
        keys = tuple(
            f"{c.target.table_id}.{c.target.column_id}" for c in field.candidates
        )
        rank = (
            keys.index(case.expected) + 1
            if case.expected is not None and case.expected in keys
            else None
        )
        result.append(
            Observation(
                case_id=case.case_id,
                language=case.language,
                group=case.group,
                rank=rank,
                positive=case.expected is not None,
                auto=field.status == "auto_candidate",
                correct=rank == 1,
                review=field.status == "review",
            )
        )
    return tuple(result)


def metrics(observations: tuple[Observation, ...]) -> EvaluationMetrics:
    positive = tuple(o for o in observations if o.positive)
    auto = tuple(o for o in observations if o.auto)

    def ratio(count: int, total: int) -> str:
        with localcontext(Context(prec=40)):
            return (
                str((Decimal(count) / total).quantize(Decimal("0.000001")))
                if total
                else "0.000000"
            )

    with localcontext(Context(prec=40)):
        mrr = sum((Decimal(1) / o.rank for o in positive if o.rank), Decimal(0)) / max(
            1, len(positive)
        )
    review = sum(o.review for o in observations)
    return EvaluationMetrics(
        cases=len(observations),
        positive_cases=len(positive),
        recall_at_1=ratio(
            sum(o.rank is not None and o.rank <= 1 for o in positive), len(positive)
        ),
        recall_at_5=ratio(
            sum(o.rank is not None and o.rank <= 5 for o in positive), len(positive)
        ),
        recall_at_10=ratio(
            sum(o.rank is not None and o.rank <= 10 for o in positive), len(positive)
        ),
        mrr=str(mrr.quantize(Decimal("0.000001"))),
        false_auto=sum(not o.correct for o in auto),
        auto_precision=ratio(sum(o.correct for o in auto), len(auto)) if auto else None,
        auto_coverage=ratio(len(auto), len(observations)),
        review_coverage=ratio(review, len(observations)),
        abstain_coverage=ratio(
            len(observations) - len(auto) - review, len(observations)
        ),
    )


def grouped_metrics(
    observations: tuple[Observation, ...],
) -> dict[str, EvaluationMetrics]:
    groups: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        groups[observation.language].append(observation)
        groups[observation.group].append(observation)
    return {name: metrics(tuple(group)) for name, group in sorted(groups.items())}


async def calibrate(cases: tuple[EvaluationCase, ...]) -> MappingWeights:
    calibration = tuple(c for c in cases if c.split == "calibration")
    ranked = []
    for weights in weight_grid():
        result = metrics(await evaluate(calibration, weights))
        key = (
            result.false_auto,
            -Decimal(result.recall_at_5),
            -Decimal(result.recall_at_1),
            (
                weights.name_similarity,
                weights.alias_match,
                weights.type_compatibility,
                weights.value_pattern_match,
                weights.structural_context,
                weights.database_relation_score,
            ),
        )
        ranked.append((key, weights))
    return min(ranked, key=lambda item: item[0])[1]
