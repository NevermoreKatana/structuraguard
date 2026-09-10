"""Статическая security boundary до чтения и применения physical source."""

from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from pydantic import BaseModel

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.analysis import (
    ExplicitRecordGrouping,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.document_semantics import (
    DocumentSpanGrouping,
    DocumentSpanSelector,
)
from structuraguard.contracts.execution import (
    ExecutionStage,
    ParseExecutionIssue,
    ParsePlanOptions,
)
from structuraguard.contracts.parsing import (
    DocumentBlockGrouping,
    DocumentParsePlan,
    DocumentTargetSelector,
    EveryLineStart,
    LogLineGrouping,
    LogParsePlan,
    ParsePlanValidationRequest,
    TabularColumnSelector,
    TabularParsePlan,
    TabularRowGrouping,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.exceptions import ParseExecutionError


def failure(
    reason: str,
    *,
    code: str = "PARSE_EXECUTION_MISMATCH",
    stage: ExecutionStage = ExecutionStage.SELECTION,
    batch_index: int | None = None,
    emitted: int = 0,
) -> ParseExecutionError:
    return ParseExecutionError(
        ParseExecutionIssue(
            code=code,
            reason=reason,
            stage=stage,
            batch_index=batch_index,
            emitted_batches=emitted,
        )
    )


def bounded_payload(
    value: object, options: ParsePlanOptions, *, maximum: int | None = None
) -> None:
    """Bounded preflight до копирования/validation; не принимает arbitrary objects."""
    maximum = maximum or options.source_limits.max_batch_bytes
    stack: list[tuple[object, int]] = [(value, 0)]
    count = size = 0
    while stack:
        item, depth = stack.pop()
        count += 1
        size += 32
        if count > options.source_limits.max_batch_items or depth > 64:
            raise failure(
                "payload_items",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )
        children: list[object] | tuple[object, ...] = ()
        if isinstance(item, str | bytes):
            size += len(item) * 4
        elif isinstance(item, tuple | list):
            children = item
        elif type(item) is dict:
            if (
                len(item) * 2 + count + len(stack)
                > options.source_limits.max_batch_items
            ):
                raise failure(
                    "payload_items",
                    code="SECURITY_LIMIT_EXCEEDED",
                    stage=ExecutionStage.LIMIT,
                )
            children = [*item.keys(), *item.values()]
        elif isinstance(item, BaseModel):
            children = [getattr(item, name) for name in type(item).model_fields]
        elif isinstance(item, int):
            size += item.bit_length() // 8
        elif isinstance(item, Decimal):
            size += len(item.as_tuple().digits)
        elif item is not None and not isinstance(item, float | date | datetime):
            raise failure(
                "payload_type",
                code="PARSE_PLAN_INVALID",
                stage=ExecutionStage.VALIDATION,
            )
        if (
            len(children) + count + len(stack) > options.source_limits.max_batch_items
            or size > maximum
        ):
            raise failure(
                "payload_bytes_or_items",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )
        stack.extend((child, depth + 1) for child in children)


def prepare(
    request: ParsePlanValidationRequest | dict[str, object], options: ParsePlanOptions
) -> ParsePlanValidationRequest:
    try:
        if type(request) not in {ParsePlanValidationRequest, dict}:
            raise ValueError("request_type")
        raw_plan = (
            request.plan
            if isinstance(request, ParsePlanValidationRequest)
            else request.get("plan")
        )
        bounded_payload(raw_plan, options, maximum=options.max_plan_bytes)
        bounded_payload(request, options)
        data = (
            request.model_dump(mode="python", warnings="error")
            if isinstance(request, ParsePlanValidationRequest)
            else request
        )
        checked = ParsePlanValidationRequest.model_validate(data)
    except (ValueError, TypeError, AttributeError):
        raise failure(
            "request_contract",
            code="PARSE_PLAN_INVALID",
            stage=ExecutionStage.VALIDATION,
        ) from None
    if (
        checked.manifest.schema_version != "1.1.0"
        or checked.profile.schema_version != "1.1.0"
    ):
        raise failure(
            "unverifiable_source_version",
            code="PARSE_PLAN_UNSUPPORTED",
            stage=ExecutionStage.VALIDATION,
        )
    if (
        canonical_sha256_value(
            checked.manifest, exclude_top_level=frozenset({"extraction_fingerprint"})
        )
        != checked.manifest.extraction_fingerprint
    ):
        raise failure(
            "manifest_fingerprint",
            code="PARSE_PLAN_SOURCE_MISMATCH",
            stage=ExecutionStage.VALIDATION,
        )
    static_plan(checked, options)
    return checked


def record_steps(grouping: TreeNodeGrouping) -> tuple[TreeStep, ...]:
    return grouping.record_steps or tuple(
        TreeStep(operation=TreePathOperation.KEY, name=name)
        for name in grouping.record_path
    )


def static_plan(request: ParsePlanValidationRequest, options: ParsePlanOptions) -> None:
    plan = request.plan

    def unsupported(reason: str) -> NoReturn:
        raise failure(
            reason, code="PARSE_PLAN_UNSUPPORTED", stage=ExecutionStage.VALIDATION
        )

    if plan.rules or any(f.rules for f in plan.fields):
        unsupported("rule_policy_required")
    if any(e.identity_field_ids for e in plan.entities):
        unsupported("identity_policy_required")
    if isinstance(plan, TabularParsePlan):
        if (
            len(plan.entities) != 1
            or not isinstance(plan.entities[0].grouping, TabularRowGrouping)
            or plan.entities[0].grouping.rows_per_entity != 1
        ):
            unsupported("tabular_grouping")
        if plan.entities[0].parent_entity_id is not None or plan.data_end_row is None:
            unsupported("tabular_finite_scope")
        if plan.data_end_row - plan.data_start_row + 1 > options.max_records:
            unsupported("row_range_limit")
        if any(
            f.selector.column_index >= options.source_limits.max_columns
            for f in plan.fields
            if isinstance(f.selector, TabularColumnSelector)
        ):
            unsupported("column_limit")
    elif isinstance(plan, TreeParsePlan):
        groups = {
            e.entity_id: e.grouping
            for e in plan.entities
            if isinstance(e.grouping, TreeNodeGrouping)
        }
        if len([e for e in plan.entities if e.parent_entity_id is None]) != 1:
            unsupported("tree_root_count")
        paths = {name: record_steps(g) for name, g in groups.items()}
        if len(set(paths.values())) != len(paths):
            unsupported("overlapping_tree_entities")
        for entity in plan.entities:
            tree_grouping = groups[entity.entity_id]
            if tree_grouping.include_descendants:
                unsupported("descendant_policy_required")
            if entity.parent_entity_id is not None:
                parent_path = paths[entity.parent_entity_id]
                if (
                    len(paths[entity.entity_id]) <= len(parent_path)
                    or paths[entity.entity_id][: len(parent_path)] != parent_path
                ):
                    unsupported("tree_parent_path")
        for field in plan.fields:
            selector = field.selector
            if not isinstance(selector, TreePathSelector) or any(
                s.operation is TreePathOperation.ITEM for s in selector.steps
            ):
                unsupported("field_cardinality")
    elif isinstance(plan, DocumentParsePlan) and all(
        isinstance(f.selector, DocumentSpanSelector) for f in plan.fields
    ):
        if not all(isinstance(e.grouping, DocumentSpanGrouping) for e in plan.entities):
            unsupported("span_grouping")
    else:
        if any(e.parent_entity_id is not None for e in plan.entities):
            unsupported("physical_group_parent")
        explicit = all(
            isinstance(e.grouping, ExplicitRecordGrouping) for e in plan.entities
        )
        if explicit:
            scope = (
                plan.line_refs if isinstance(plan, LogParsePlan) else plan.block_refs
            )
            positions = {ref: i for i, ref in enumerate(scope)}
            for entity in plan.entities:
                grouping = entity.grouping
                if not isinstance(grouping, ExplicitRecordGrouping):
                    continue
                for record in grouping.records:
                    indices = [positions[ref] for ref in record]
                    if indices != list(range(indices[0], indices[0] + len(indices))):
                        unsupported("overlapping_or_unordered_records")
        elif len(plan.entities) != 1:
            unsupported("ambiguous_legacy_variants")
        elif isinstance(plan, LogParsePlan):
            grouping = plan.entities[0].grouping
            if not isinstance(grouping, LogLineGrouping) or not isinstance(
                grouping.start, EveryLineStart
            ):
                unsupported("log_start_policy")
        elif not isinstance(plan.entities[0].grouping, DocumentBlockGrouping):
            unsupported("document_grouping")
        if isinstance(plan, DocumentParsePlan) and any(
            isinstance(f.selector, DocumentTargetSelector)
            and f.selector.target == "table_column"
            for f in plan.fields
        ):
            unsupported("document_table_scope_requires_tabular_plan")
        if isinstance(plan, LogParsePlan) and plan.max_lines_per_record > 64:
            unsupported("record_line_limit")


def validation_fingerprint(
    request: ParsePlanValidationRequest, options: ParsePlanOptions
) -> str:
    return canonical_sha256_value(
        (
            "parse_plan_validator",
            "1.0.0",
            request.plan.fingerprint,
            request.profile.profile_fingerprint,
            request.manifest.extraction_fingerprint,
            options.canonical_json(),
        )
    )
