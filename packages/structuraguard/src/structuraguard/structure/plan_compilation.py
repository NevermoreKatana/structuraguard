"""Компиляция закрытого semantic proposal в существующую grammar ParsePlan."""

import json
import re
from decimal import Decimal

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.analysis import (
    ExplicitRecordGrouping,
    LogRecordSelector,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.common import PhysicalObjectKind, ProducerMetadata
from structuraguard.contracts.llm import LLMErrorCode, LLMPlanProvenance
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    DocumentTargetSelector,
    LogParsePlan,
    LogTokenSelector,
    ParseEntity,
    ParseField,
    ParseFieldSelector,
    ParsePlan,
    StructureAnalysisRequest,
    TabularColumnSelector,
    TabularParsePlan,
    TabularRowGrouping,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.contracts.semantic import (
    LLMStructurePolicy,
    LLMStructureSuggestion,
    SemanticPathStep,
    SemanticSelector,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.structure.semantic_samples import SemanticSampleCatalog


def reject_active_content(text: str) -> None:
    """Консервативный veto известных code/command/injection fragments.

    Главная защита — закрытая grammar и source membership: строки никогда не
    получают execution capability. Regex — дополнительный veto, не sandbox.
    """
    pattern = r"(?is)(```|<script\b|\$\(|\b(?:eval|exec|compile|__import__|os\.system|subprocess\.\w+)\s*\(|\b(?:select\b.{0,256}?\bfrom|insert\s+into|delete\s+from|(?:drop|alter|create|truncate)\s+(?:table|database)|update\s+\w+\s+set)\b|\b(?:import\s+(?:os|sys|subprocess)|from\s+\w+\s+import|def\s+\w+\s*\(|(?:curl|wget|bash|powershell|cmd\.exe)\s+|rm\s+-|python[0-9.]*\s+-c)|ignore\s+(?:all\s+)?(?:previous|system)\s+instructions|игнорируй\s+(?:все\s+)?(?:предыдущие|системные)\s+инструкции)"
    try:
        pending: list[object] = [json.loads(text)]
    except (ValueError, RecursionError):
        pending = [text]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and re.search(pattern, value):
            raise LLMProviderError(LLMErrorCode.UNSAFE_CONTENT)


def _steps(path: tuple[SemanticPathStep, ...]) -> tuple[TreeStep, ...]:
    return tuple(
        TreeStep(
            operation=TreePathOperation(step.operation),
            name=step.name,
            occurrence=step.occurrence,
        )
        for step in path
    )


def _selector(value: SemanticSelector) -> ParseFieldSelector:
    nullable = value.model_dump(exclude={"kind", "path"})
    active = {key for key, item in nullable.items() if item is not None}
    required = {
        "column": {"index"},
        "tree": {"value_source"},
        "log_piece": {"index", "offset", "delimiter"},
        "log_record": set(),
        "document": {"offset", "target"},
    }
    expected = required[value.kind]
    if value.kind == "document" and value.target == "value":
        expected = {*expected, "key_equals"}
    if active != expected or (value.path and value.kind != "tree"):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
    if value.kind == "column":
        assert value.index is not None
        return TabularColumnSelector(column_index=value.index)
    if value.kind == "tree":
        assert value.value_source is not None
        return TreePathSelector(
            steps=_steps(value.path), value_source=value.value_source
        )
    if value.kind == "log_piece":
        assert (
            value.index is not None
            and value.offset is not None
            and value.delimiter is not None
        )
        return LogTokenSelector(
            token_index=value.index, line_offset=value.offset, delimiter=value.delimiter
        )
    if value.kind == "log_record":
        return LogRecordSelector()
    assert value.target is not None and value.offset is not None
    return DocumentTargetSelector(
        target=value.target, block_offset=value.offset, key_equals=value.key_equals
    )


def proposal_confidence(
    suggestion: LLMStructureSuggestion, catalog: SemanticSampleCatalog
) -> Decimal:
    """Оценка от bound deterministic candidates; model self-confidence игнорируется."""
    if any(alias not in catalog.candidates for alias in suggestion.candidate_ids):
        raise LLMProviderError(LLMErrorCode.UNKNOWN_SOURCE_REFERENCE)
    chosen = [catalog.candidates[alias] for alias in suggestion.candidate_ids]
    if suggestion.plan is not None:
        if any(c.plan_kind.value != suggestion.plan.kind for c in chosen):
            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
        if not chosen:
            chosen = [
                c
                for c in catalog.candidates.values()
                if c.plan_kind.value == suggestion.plan.kind
            ]
    return min((c.confidence for c in chosen), default=Decimal(0))


def compile_plan(
    suggestion: LLMStructureSuggestion,
    request: StructureAnalysisRequest,
    catalog: SemanticSampleCatalog,
    policy: LLMStructurePolicy,
    provenance: LLMPlanProvenance,
) -> ParsePlan:
    """Разрешить aliases/paths, проверить grammar и назначить собственные lineage."""
    proposed = suggestion.plan
    if proposed is None:
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
    if (
        proposed.kind not in policy.allowed_kinds
        or len(proposed.fields) > policy.max_fields
        or len(proposed.entities) > policy.max_entities
    ):
        raise LLMProviderError(LLMErrorCode.CAPABILITY_MISMATCH)
    reject_active_content(proposed.canonical_json())
    fields: list[ParseField] = []
    entities: list[ParseEntity] = []
    try:
        for field in proposed.fields:
            if (
                field.locale_hint is not None
                and field.locale_hint not in policy.allowed_locales
            ):
                raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
            fields.append(
                ParseField(
                    field_id=field.field_id,
                    semantic_name=field.semantic_name,
                    semantic_type=field.semantic_type,
                    locale_hint=field.locale_hint,
                    source_refs=tuple(
                        catalog.resolve(ref) for ref in field.source_refs
                    ),
                    selector=_selector(field.selector),
                )
            )
        for entity in proposed.entities:
            grouping: TabularRowGrouping | TreeNodeGrouping | ExplicitRecordGrouping
            if proposed.kind == "tabular":
                if entity.path or entity.records:
                    raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                grouping = TabularRowGrouping()
            elif proposed.kind == "tree":
                if entity.records:
                    raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                grouping = TreeNodeGrouping(record_steps=_steps(entity.path))
            else:
                if entity.path:
                    raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                grouping = ExplicitRecordGrouping(
                    records=tuple(
                        tuple(catalog.resolve(ref) for ref in record)
                        for record in entity.records
                    )
                )
            entities.append(
                ParseEntity(
                    entity_id=entity.entity_id,
                    entity_type=entity.entity_type,
                    field_ids=entity.field_ids,
                    grouping=grouping,
                    parent_entity_id=entity.parent_entity_id,
                )
            )
        evidence = tuple(
            dict.fromkeys(ref for field in fields for ref in field.source_refs)
        )
        common = dict(
            plan_id="llm_" + canonical_sha256_value(proposed)[-24:],
            schema_version="1.1.0",
            revision=1,
            source_fingerprint=request.source.source_fingerprint,
            extraction_fingerprint=request.manifest.extraction_fingerprint,
            profile_fingerprint=request.profile.profile_fingerprint,
            confidence=proposal_confidence(suggestion, catalog),
            producer=ProducerMetadata(
                component_id="llm_structure_analyzer",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            semantic_analysis=provenance,
            fields=tuple(fields),
            entities=tuple(entities),
            evidence=evidence,
        )
        plan: ParsePlan
        row_values = (
            proposed.header_row,
            proposed.data_start_row,
            proposed.data_end_row,
            proposed.footer_start_row,
        )
        if proposed.kind != "tabular" and (
            any(value is not None for value in row_values)
            or proposed.repeated_header_rows
        ):
            raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
        if proposed.kind in {"tabular", "tree"}:
            if proposed.root_ref is None or proposed.scope:
                raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
            root = catalog.resolve(proposed.root_ref)
            if proposed.kind == "tabular":
                plan = TabularParsePlan.model_validate(
                    {
                        **common,
                        "table_ref": root,
                        "header_row": proposed.header_row,
                        "data_start_row": proposed.data_start_row,
                        "data_end_row": proposed.data_end_row,
                        "footer_start_row": proposed.footer_start_row,
                        "repeated_header_rows": proposed.repeated_header_rows,
                    }
                )
            else:
                roots = [
                    entity
                    for entity in proposed.entities
                    if entity.parent_entity_id is None
                ]
                if len(roots) != 1:
                    raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
                paths = [
                    entry.path
                    for entry in catalog.entries.values()
                    if entry.ref.kind
                    in {PhysicalObjectKind.TREE_NODE, PhysicalObjectKind.VALUE}
                ]
                root_path = catalog.entries[proposed.root_ref].path
                for entity in proposed.entities:
                    record_path = (*root_path, *entity.path)
                    if not any(
                        path[: len(record_path)] == record_path for path in paths
                    ):
                        raise LLMProviderError(LLMErrorCode.UNKNOWN_SOURCE_REFERENCE)
                    for field in proposed.fields:
                        if field.field_id in entity.field_ids:
                            target_path = (*record_path, *field.selector.path)
                            if target_path not in paths:
                                raise LLMProviderError(
                                    LLMErrorCode.UNKNOWN_SOURCE_REFERENCE
                                )
                plan = TreeParsePlan.model_validate(
                    {**common, "root_ref": root, "record_steps": _steps(roots[0].path)}
                )
        else:
            if proposed.root_ref is not None:
                raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION)
            scope = tuple(catalog.resolve(ref) for ref in proposed.scope)
            if proposed.kind == "log":
                maximum = max(
                    (
                        len(record)
                        for entity in proposed.entities
                        for record in entity.records
                    ),
                    default=1,
                )
                plan = LogParsePlan.model_validate(
                    {**common, "line_refs": scope, "max_lines_per_record": maximum}
                )
            else:
                plan = DocumentParsePlan.model_validate({**common, "block_refs": scope})
        if len(plan.canonical_json().encode()) > policy.execution.max_plan_bytes:
            raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
        return plan
    except (ValueError, TypeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None


def complete_scope(plan: ParsePlan, catalog: SemanticSampleCatalog) -> bool:
    """Не принять bounded log/document sample за полный semantic scope."""
    families = catalog.families
    if isinstance(plan, LogParsePlan):
        return (
            PhysicalObjectKind.LINE not in catalog.unindexed_scope
            and set(plan.line_refs) == catalog.source_scope[PhysicalObjectKind.LINE]
            and not catalog.has_unwrapped_blocks
            and not families.intersection(
                {PhysicalObjectKind.TABLE, PhysicalObjectKind.TREE_NODE}
            )
        )
    if isinstance(plan, DocumentParsePlan):
        return (
            PhysicalObjectKind.BLOCK not in catalog.unindexed_scope
            and set(plan.block_refs) == catalog.source_scope[PhysicalObjectKind.BLOCK]
            and not families.intersection(
                {PhysicalObjectKind.TABLE, PhysicalObjectKind.TREE_NODE}
            )
        )
    if isinstance(plan, TabularParsePlan):
        entry = next(
            entry for entry in catalog.entries.values() if entry.ref == plan.table_ref
        )
        return (
            catalog.table_count == 1
            and (
                plan.footer_start_row is not None
                or plan.data_end_row == entry.details["last_row"]
            )
            and not families.intersection(
                {
                    PhysicalObjectKind.BLOCK,
                    PhysicalObjectKind.LINE,
                    PhysicalObjectKind.TREE_NODE,
                }
            )
            and all(
                ref.local_id == plan.table_ref.local_id
                for ref in catalog.source_scope[PhysicalObjectKind.TABLE]
            )
        )
    return not families.intersection(
        {PhysicalObjectKind.TABLE, PhysicalObjectKind.BLOCK, PhysicalObjectKind.LINE}
    ) and catalog.tree_roots == {plan.root_ref}
