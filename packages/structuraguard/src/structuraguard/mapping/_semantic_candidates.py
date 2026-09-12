"""Bounded M9 composition: таблицы, column aliases и полные FK candidates."""

import asyncio
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    DeterministicMappingResult,
    MappingScope,
)
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog
from structuraguard.contracts.semantic_mapping import (
    SemanticColumnCandidate,
    SemanticFieldCandidates,
    SemanticMappingCandidateSet,
    SemanticMappingOptions,
    SemanticMappingPreparation,
    SemanticRelationCandidate,
    SemanticRelationPair,
    SemanticTableCandidate,
)
from structuraguard.profiling.pii import maximum_classification

from ._inputs import Budget, bounded_size, failure, validate_inputs
from .mapper import DeterministicMapper


def check_deadline(deadline: float) -> None:
    """Проверить время после синхронной работы, до запуска timeout callback."""
    if asyncio.get_running_loop().time() >= deadline:
        raise TimeoutError


def checked_options(options: SemanticMappingOptions | None) -> SemanticMappingOptions:
    """Проверить configuration до serialization, включая forged frozen DTO."""
    try:
        value = options or SemanticMappingOptions()
        bounded_size(value, 65536)
        return SemanticMappingOptions.model_validate(value.model_dump(warnings="error"))
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise failure("MAPPING_INPUT_INVALID", "semantic_options") from None


def _components(profile: NormalizedDataProfile) -> list[tuple[str, ...]]:
    adjacency = {f.field.entity_type: set[str]() for f in profile.fields}
    for r in profile.relationships:
        if r.kind == "parent_child":
            a, b = r.left.entity_type, r.right.entity_type
            adjacency[a].add(b)
            adjacency[b].add(a)
    result = []
    unseen = set(adjacency)
    while unseen:
        pending = [min(unseen)]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency[current] - seen)
        unseen -= seen
        result.append(tuple(sorted(seen)))
    return result


def _tables(
    fields: tuple[SemanticFieldCandidates, ...],
    options: SemanticMappingOptions,
    budget: Budget,
) -> tuple[tuple[SemanticTableCandidate, ...], bool]:
    result: list[SemanticTableCandidate] = []
    pruned = False
    for entity_id in sorted({f.entity_id for f in fields}):
        rows = [f for f in fields if f.entity_id == entity_id]
        table_ids = {c.target.table_id for f in rows for c in f.ranked.candidates}
        scores = []
        budget.work(len(table_ids) * sum(len(f.ranked.candidates) + 1 for f in rows))
        for table_id in sorted(table_ids):
            best = [
                max(
                    (
                        e.base_score
                        for c, e in zip(
                            f.ranked.candidates, f.ranked.explanations, strict=True
                        )
                        if c.target.table_id == table_id
                    ),
                    default=Decimal(0),
                )
                for f in rows
            ]
            with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
                score = (sum(best, Decimal(0)) / len(rows)).quantize(
                    Decimal("0.000001")
                )
            scores.append((table_id, score))
        scores.sort(key=lambda pair: (-pair[1], pair[0]))
        pruned |= len(scores) > options.top_k
        for table_id, score in scores[: options.top_k]:
            result.append(
                SemanticTableCandidate(
                    candidate_id=f"t{len(result)}",
                    source_id=entity_id,
                    table_id=table_id,
                    base_score=score,
                )
            )
    return tuple(result), pruned


def _relations(
    tables: tuple[SemanticTableCandidate, ...],
    columns: tuple[SemanticColumnCandidate, ...],
    profile: NormalizedDataProfile,
    entities: tuple[str, ...],
    catalog: DatabaseCatalog,
    options: SemanticMappingOptions,
    budget: Budget,
) -> tuple[tuple[SemanticRelationCandidate, ...], tuple[str, ...], bool]:
    graph = catalog.dependency_graph
    assert graph is not None
    indexes = {name: f"e{i}" for i, name in enumerate(entities)}
    source_contexts = {
        (indexes[r.left.entity_type], indexes[r.right.entity_type])
        for r in profile.relationships
        if r.kind == "parent_child"
        and r.left.entity_type in indexes
        and r.right.entity_type in indexes
    }
    source_links = {
        (r.left, r.right) for r in profile.relationships if r.kind == "parent_child"
    }
    by_table: dict[str, list[SemanticTableCandidate]] = {}
    for table in tables:
        by_table.setdefault(table.table_id, []).append(table)
    candidates: list[SemanticRelationCandidate] = []
    for edge in graph.edges:
        budget.work(1)
        for parent in by_table.get(edge.parent_table_id, ()):
            for child in by_table.get(edge.child_table_id, ()):
                budget.work(1)
                pair_context = (parent.source_id, child.source_id)
                if (
                    parent.source_id != child.source_id
                    and pair_context not in source_contexts
                ):
                    continue
                pairs = []
                values: list[Decimal] = []
                for p, c in zip(
                    edge.parent_column_ids, edge.child_column_ids, strict=True
                ):
                    budget.work(2 * len(columns))
                    pa = [
                        x
                        for x in columns
                        if x.table_candidate_id == parent.candidate_id
                        and x.mapping.target.column_id == p
                    ]
                    ca = [
                        x
                        for x in columns
                        if x.table_candidate_id == child.candidate_id
                        and x.mapping.target.column_id == c
                    ]
                    if parent.source_id != child.source_id:
                        # Межтиповой FK требует source evidence каждого компонента.
                        budget.work(len(pa) * len(ca))
                        matches = [
                            (a, b)
                            for a in pa
                            for b in ca
                            if (a.mapping.source, b.mapping.source) in source_links
                        ]
                        pa = [a for a in pa if any(a == x for x, _ in matches)]
                        ca = [b for b in ca if any(b == y for _, y in matches)]
                    if not pa or not ca:
                        break
                    budget.work(len(pa) * len(ca))
                    allowed_pairs = tuple(
                        (a.candidate_id, b.candidate_id)
                        for a in pa
                        for b in ca
                        if a.mapping.source != b.mapping.source
                        and (
                            parent.source_id == child.source_id
                            or (a.mapping.source, b.mapping.source) in source_links
                        )
                    )
                    if not allowed_pairs:
                        break
                    pairs.append(
                        SemanticRelationPair(
                            parent_candidates=tuple(x.candidate_id for x in pa),
                            child_candidates=tuple(x.candidate_id for x in ca),
                            allowed_pairs=allowed_pairs,
                        )
                    )
                    values.extend(
                        (
                            max(x.explanation.base_score for x in pa),
                            max(x.explanation.base_score for x in ca),
                        )
                    )
                if len(pairs) != len(edge.parent_column_ids):
                    continue
                candidates.append(
                    SemanticRelationCandidate(
                        candidate_id=f"r{len(candidates)}",
                        source_id="r0",
                        parent_entity_id=parent.source_id,
                        child_entity_id=child.source_id,
                        parent_table_candidate_id=parent.candidate_id,
                        child_table_candidate_id=child.candidate_id,
                        foreign_key=edge,
                        pairs=tuple(pairs),
                        base_score=min(values),
                        requires_strategy=any(
                            edge in cycle.foreign_keys for cycle in graph.cycles
                        ),
                    )
                )
                budget.retain(bounded_size(candidates[-1], options.max_state_bytes))
    # Отдельный context на пару target tables допускает split по цепочке FK.
    contexts = {
        (
            r.parent_entity_id,
            r.child_entity_id,
            r.parent_table_candidate_id,
            r.child_table_candidate_id,
        )
        for r in candidates
    }
    represented = {(a, b) for a, b, _, _ in contexts}
    contexts.update((a, b, "", "") for a, b in source_contexts - represented)
    if len(contexts) > options.max_relations:
        raise failure("MAPPING_LIMIT_EXCEEDED", "relation_contexts")
    result: list[SemanticRelationCandidate] = []
    pruned = False
    for i, context in enumerate(sorted(contexts)):
        selected = [
            r
            for r in candidates
            if (
                r.parent_entity_id,
                r.child_entity_id,
                r.parent_table_candidate_id,
                r.child_table_candidate_id,
            )
            == context
        ]
        selected.sort(
            key=lambda r: (
                -r.base_score,
                r.foreign_key.child_table_id,
                r.foreign_key.foreign_key_id,
                r.candidate_id,
            )
        )
        pruned |= len(selected) > options.top_k
        for candidate in selected[: options.top_k]:
            result.append(
                candidate.model_copy(
                    update={"source_id": f"r{i}", "candidate_id": f"r{len(result)}"}
                )
            )
    return tuple(result), tuple(f"r{i}" for i in range(len(contexts))), pruned


def _group(
    index: int,
    entities: tuple[str, ...],
    ranked: DeterministicMappingResult,
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    options: SemanticMappingOptions,
    budget: Budget,
) -> SemanticMappingCandidateSet:
    fields = tuple(
        SemanticFieldCandidates(
            source_id=f"f{i}",
            entity_id=f"e{entities.index(f.source.entity_type)}",
            ranked=f,
        )
        for i, f in enumerate(
            f for f in ranked.fields if f.source.entity_type in entities
        )
    )
    tables, pruned = _tables(fields, options, budget)
    lookup = {(t.source_id, t.table_id): t.candidate_id for t in tables}
    columns: list[SemanticColumnCandidate] = []
    for field in fields:
        for mapping, explanation in zip(
            field.ranked.candidates, field.ranked.explanations, strict=True
        ):
            target = lookup.get((field.entity_id, mapping.target.table_id))
            if target is not None:
                columns.append(
                    SemanticColumnCandidate(
                        candidate_id=f"c{len(columns)}",
                        source_id=field.source_id,
                        table_candidate_id=target,
                        mapping=mapping,
                        explanation=explanation,
                    )
                )
    relations, sources, relation_pruned = _relations(
        tables, tuple(columns), profile, entities, catalog, options, budget
    )
    reasons = ["CANDIDATES_PRUNED"] if pruned or relation_pruned else []
    if any(not f.pii.complete or f.pii.state == "unknown" for f in profile.fields):
        reasons.append("CLASSIFICATION_INCOMPLETE")
    group = SemanticMappingCandidateSet(
        group_id=f"g{index}",
        entity_types=entities,
        fields=fields,
        tables=tables,
        columns=tuple(columns),
        relations=relations,
        relation_sources=sources,
        reasons=tuple(reasons),
        payload_json="",
        fingerprint="sha256:" + "0" * 64,
    )
    budget.retain(bounded_size(group, options.max_state_bytes))
    return group.model_copy(
        update={
            "fingerprint": canonical_sha256_value(
                group, exclude_top_level=frozenset({"fingerprint", "payload_json"})
            )
        }
    )


async def prepare_semantic_mapping(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    *,
    scope: MappingScope,
    semantic_catalog: DatabaseSemanticCatalog | None = None,
    options: SemanticMappingOptions | None = None,
    ranking_options: DeterministicMappingOptions | None = None,
) -> SemanticMappingPreparation:
    """Подготовить ограниченные группы candidates без scanner, LLM и DB I/O.

    Args:
        profile: Завершённый профиль M8 schema 1.0.0.
        catalog: Каталог M7 schema 1.1.0 / catalog-v1 с проверяемым fingerprint.
        scope: Явные allow/deny refs, привязанные к target/policy каталога.
        semantic_catalog: Необязательные semantic annotations тех же snapshots.
        options: Бюджеты групп/проекции M10; None включает defaults.
        ranking_options: Параметры M9; эффективный top-k — минимум его и M10 top-k.

    Returns:
        Чувствительный SemanticMappingPreparation с M9 lineage, полными ordered
        FK и отдельным masked payload. Полный catalog и raw examples в payload
        отсутствуют. Результат подготовки сам по себе не разрешает egress/load.

    Raises:
        MappingError: Неверные входы/bindings, несовместимая schema или превышение
            budgets/deadline; частичный результат не возвращается.
        DatabaseInspectionError: DATABASE_SCHEMA_DRIFT при неверном catalog hash.
        asyncio.CancelledError: Отмена без фоновых задач и частичного результата.
    """
    try:
        return await _prepare_semantic_mapping(
            profile,
            catalog,
            scope=scope,
            semantic_catalog=semantic_catalog,
            options=options,
            ranking_options=ranking_options,
        )
    except TimeoutError:
        raise failure(
            "MAPPING_LIMIT_EXCEEDED", "semantic_preparation_deadline"
        ) from None


async def _prepare_semantic_mapping(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    *,
    scope: MappingScope,
    semantic_catalog: DatabaseSemanticCatalog | None,
    options: SemanticMappingOptions | None,
    ranking_options: DeterministicMappingOptions | None,
) -> SemanticMappingPreparation:
    from ._semantic_prompt import group_payload

    checked = checked_options(options)
    mapper = DeterministicMapper(
        ranking_options or DeterministicMappingOptions(top_k=checked.top_k)
    )
    if mapper.options.top_k > checked.top_k:
        # Отдельные веса M9 не должны расширять разрешённый объём prompt M10.
        mapper = DeterministicMapper(
            mapper.options.model_copy(update={"top_k": checked.top_k})
        )
    deadline = asyncio.get_running_loop().time() + checked.max_seconds
    async with asyncio.timeout_at(deadline):
        profile, catalog, scope, semantic, _ = validate_inputs(
            profile, catalog, scope, semantic_catalog, mapper.options
        )
        components = _components(profile)
        if len(components) > checked.max_groups or any(
            len(c) > checked.max_entities
            or sum(f.field.entity_type in c for f in profile.fields)
            > checked.max_fields
            for c in components
        ):
            raise failure("MAPPING_LIMIT_EXCEEDED", "entity_group")
        ranked = await mapper.rank(
            profile, catalog, scope=scope, semantic_catalog=semantic
        )
        check_deadline(deadline)
        budget = Budget(
            DeterministicMappingOptions(
                max_operations=checked.max_operations,
                max_state_bytes=checked.max_state_bytes,
            )
        )
        groups = []
        for index, entities in enumerate(components):
            await asyncio.sleep(0)
            check_deadline(deadline)
            group = _group(index, entities, ranked, profile, catalog, checked, budget)
            check_deadline(deadline)
            payload = group_payload(group, profile, catalog, semantic, checked)
            budget.retain(len(payload.encode()) * 4)
            groups.append(group.model_copy(update={"payload_json": payload}))
            check_deadline(deadline)
        classification = maximum_classification(
            profile.classification, *(f.pii.classification for f in profile.fields)
        )
        if any(not f.pii.complete or f.pii.state == "unknown" for f in profile.fields):
            classification = DataClassification.RESTRICTED
        result = SemanticMappingPreparation(
            deterministic=ranked, groups=tuple(groups), classification=classification
        )
        check_deadline(deadline)
        return result
