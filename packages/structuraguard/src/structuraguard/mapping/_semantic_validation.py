"""Модель не расширяет candidate set и не подтверждает корректность своих refs."""

from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.semantic_mapping import (
    SemanticAssessment,
    SemanticMappingCandidateSet,
    SemanticMappingDecision,
    SemanticMappingOptions,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm._content import reject_active_content
from structuraguard.llm._structured import parse_object

from ._inputs import bounded_size, failure


def _invalid(reason: str) -> None:
    raise failure("SEMANTIC_MAPPING_DECISION_INVALID", reason)


def _coverage(actual: tuple[str, ...], expected: tuple[str, ...]) -> None:
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        _invalid("source_or_candidate_membership")


def _assessments(
    values: tuple[SemanticAssessment, ...],
    expected: tuple[str, ...],
    selected: tuple[str, ...],
) -> None:
    _coverage(tuple(v.candidate_id for v in values), expected)
    if not set(selected) <= set(expected):
        _invalid("selected_candidate_not_listed")


def validate_decision(
    raw: str, group: SemanticMappingCandidateSet, options: SemanticMappingOptions
) -> SemanticMappingDecision:
    """Отвергнуть malformed/schema/identifier/assignment ошибки целиком, без repair."""
    try:
        if (
            type(raw) is not str
            or len(raw) > options.max_response_bytes
            or len(raw.encode()) > options.max_response_bytes
        ):
            raise ValueError
        parsed = parse_object(raw)
        bounded_size(parsed, options.max_response_bytes * 64)
        reject_active_content(raw)
        decision = SemanticMappingDecision.model_validate_json(raw, strict=True)
    except (ValueError, TypeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None
    if (
        decision.group_id != group.group_id
        or decision.candidate_set_fingerprint != group.fingerprint
    ):
        _invalid("candidate_set_binding")
    _coverage(
        tuple(x.source_id for x in decision.tables),
        tuple(f"e{i}" for i in range(len(group.entity_types))),
    )
    _coverage(
        tuple(x.source_id for x in decision.columns),
        tuple(x.source_id for x in group.fields),
    )
    _coverage(tuple(x.source_id for x in decision.relations), group.relation_sources)
    for table in decision.tables:
        _assessments(
            table.assessments,
            tuple(
                t.candidate_id for t in group.tables if t.source_id == table.source_id
            ),
            table.selected_candidate_ids,
        )
    for choice in (*decision.columns, *decision.relations):
        expected = (
            tuple(
                c.candidate_id for c in group.columns if c.source_id == choice.source_id
            )
            if choice in decision.columns
            else tuple(
                r.candidate_id
                for r in group.relations
                if r.source_id == choice.source_id
            )
        )
        _assessments(
            choice.assessments,
            expected,
            (choice.selected_candidate_id,) if choice.selected_candidate_id else (),
        )
    selected_tables = {
        t.source_id: set(t.selected_candidate_ids) for t in decision.tables
    }
    selected_columns = {
        c.selected_candidate_id for c in decision.columns if c.selected_candidate_id
    }
    mapped = [c for c in group.columns if c.candidate_id in selected_columns]
    if len({c.mapping.target for c in mapped}) != len(mapped):
        _invalid("target_collision")
    fields = {f.source_id: f for f in group.fields}
    for column in mapped:
        if (
            column.table_candidate_id
            not in selected_tables[fields[column.source_id].entity_id]
        ):
            _invalid("column_table_mismatch")
    for entity, table_ids in selected_tables.items():
        used = {
            c.table_candidate_id
            for c in mapped
            if fields[c.source_id].entity_id == entity
        }
        if used != table_ids:
            _invalid("table_without_selected_fields")
    selected_relations = {
        r.selected_candidate_id for r in decision.relations if r.selected_candidate_id
    }
    relations = [r for r in group.relations if r.candidate_id in selected_relations]
    for relation in relations:
        if (
            relation.parent_table_candidate_id
            not in selected_tables[relation.parent_entity_id]
            or relation.child_table_candidate_id
            not in selected_tables[relation.child_entity_id]
        ):
            _invalid("relation_table_mismatch")
        if any(
            not any(
                a in selected_columns and b in selected_columns
                for a, b in pair.allowed_pairs
            )
            for pair in relation.pairs
        ):
            _invalid("incomplete_composite_relation")
    for entity, table_ids in selected_tables.items():
        if len(table_ids) < 2:
            continue
        adjacency = {t: set[str]() for t in table_ids}
        for relation in relations:
            if relation.parent_entity_id == relation.child_entity_id == entity:
                a, b = (
                    relation.parent_table_candidate_id,
                    relation.child_table_candidate_id,
                )
                adjacency[a].add(b)
                adjacency[b].add(a)
        pending, seen = [min(table_ids)], set[str]()
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency[current] - seen)
        if seen != table_ids:
            _invalid("unrelated_entity_split")
    return decision
