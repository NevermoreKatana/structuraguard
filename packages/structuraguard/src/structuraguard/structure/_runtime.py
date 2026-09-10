"""Bounded физические selections: общий проход validator/executor, без I/O."""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.analysis import (
    ExplicitRecordGrouping,
    LogRecordSelector,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.common import (
    BytesScalar,
    NormalizedScalar,
    PhysicalObjectKind,
    PhysicalSourceRef,
    RawScalar,
    StringScalar,
)
from structuraguard.contracts.document_semantics import (
    DocumentSpanGrouping,
    DocumentSpanSelector,
    SourceTextSpan,
)
from structuraguard.contracts.execution import (
    ExecutionStage,
    ParsePlanOptions,
    PhysicalValueOrigin,
    SelectionOperation,
)
from structuraguard.contracts.parsing import (
    DocumentBlockGrouping,
    DocumentParsePlan,
    DocumentTargetSelector,
    LogLineGrouping,
    LogParsePlan,
    LogTokenSelector,
    ParseEntity,
    ParseField,
    ParsePlanValidationRequest,
    TabularColumnSelector,
    TabularParsePlan,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
    _iter_plan_refs,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBlock,
    ExtractedCell,
    ExtractedLine,
    ExtractedTreeNode,
    PhysicalNodeKind,
)
from structuraguard.exceptions import SecurityPolicyError, StructuralProfilingError
from structuraguard.structure._plan_check import failure, record_steps
from structuraguard.structure._stream import StreamCheck
from structuraguard.structure.text_sources import text_sources


@dataclass(frozen=True, slots=True)
class SelectedValue:
    field: ParseField
    raw: RawScalar
    normalized: NormalizedScalar
    origins: tuple[PhysicalValueOrigin, ...]
    operation: SelectionOperation = SelectionOperation.COPY


@dataclass(frozen=True, slots=True)
class SelectedEntity:
    definition: ParseEntity
    values: tuple[SelectedValue, ...]
    parent: int | None = None


@dataclass(frozen=True, slots=True)
class SelectedRecord:
    entities: tuple[SelectedEntity, ...]


def raw_size(value: RawScalar) -> int:
    raw = value.value
    if isinstance(raw, str | bytes):
        return len(raw) * 4
    if isinstance(raw, int):
        return raw.bit_length() // 8 + 32
    return len(str(raw)) * 4 + 32


def copied(field: ParseField, origin: PhysicalValueOrigin) -> SelectedValue:
    if isinstance(origin.raw_value, BytesScalar):
        raise failure("bytes_conversion_policy_required")
    if field.semantic_type == "money" and origin.raw_value.kind != "decimal":
        raise failure("money_requires_native_decimal")
    return SelectedValue(field, origin.raw_value, origin.raw_value, (origin,))


def tokens(text: str, delimiter: str, maximum: int) -> list[str]:
    separators = {"tab": "\t", "comma": ",", "pipe": "|", "colon": ":", "equals": "="}
    result = (
        text.split(maxsplit=maximum)
        if delimiter == "whitespace"
        else text.split(separators[delimiter], maxsplit=maximum)
    )
    if len(result) > maximum:
        raise failure(
            "token_limit", code="SECURITY_LIMIT_EXCEEDED", stage=ExecutionStage.LIMIT
        )
    return result


class _RecordBudget:
    """Инкрементальный бюджет: fan-out не материализуется до проверки limits."""

    def __init__(self, options: ParsePlanOptions) -> None:
        self.options = options
        self.items = 0
        self.size = 0

    def entity(self) -> None:
        self.items += 1
        self._check()

    def append(self, values: list[SelectedValue], value: SelectedValue) -> None:
        if value.field.semantic_type == "money" and value.normalized.kind != "decimal":
            raise failure("money_requires_native_decimal")
        self.items += len(value.origins) + 1
        self.size += raw_size(value.normalized) + raw_size(value.raw)
        for origin in value.origins:
            self.size += (
                raw_size(origin.raw_value)
                + len(origin.location.canonical_json().encode("utf-8"))
                + 256
            )
        self._check()
        values.append(value)

    def _check(self) -> None:
        if (
            self.items > self.options.max_record_items
            or self.size > self.options.max_record_bytes
        ):
            raise failure(
                "record_budget",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )


class PlanRuntime:
    """Удерживает batch и незавершённый record, но не весь source."""

    def __init__(
        self, request: ParsePlanValidationRequest, options: ParsePlanOptions
    ) -> None:
        self.request = request
        self.plan = request.plan
        self.options = options
        self.check = StreamCheck(options.source_limits)
        self.required = set(_iter_plan_refs(self.plan))
        self.field_seen: dict[str, set[PhysicalSourceRef]] = {
            f.field_id: set() for f in self.plan.fields
        }
        self.header: tuple[tuple[int, RawScalar], ...] | None = None
        self.data_rows = 0
        self.footer_seen = False
        self.record_count = 0
        self.total_items = 0
        self.batch_index = 0
        self.root_seen = False
        self.tree_path_seen = False
        self.tree_buffer: list[tuple[ExtractedTreeNode, PhysicalSourceRef]] = []
        self.tree_bytes = 0
        self.tree_buffer_ids: set[str] = set()
        self.root_stamp: tuple[str, str | None, str, str | None, str] | None = None
        self.tree_root_counts: dict[str, int] = {}
        self.span_values: dict[SourceTextSpan, tuple[str, PhysicalValueOrigin]] = {}
        self.is_span_plan = isinstance(self.plan, DocumentParsePlan) and all(
            isinstance(f.selector, DocumentSpanSelector) for f in self.plan.fields
        )
        self.span_index: dict[PhysicalSourceRef, set[SourceTextSpan]] = {}
        self.span_bytes = 0
        if self.is_span_plan:
            for field in self.plan.fields:
                assert isinstance(field.selector, DocumentSpanSelector)
                for span in field.selector.spans:
                    self.span_index.setdefault(span.source_ref, set()).add(span)
            for entity in self.plan.entities:
                assert isinstance(entity.grouping, DocumentSpanGrouping)
                span = entity.grouping.anchor
                self.span_index.setdefault(span.source_ref, set()).add(span)
        self.scope_position = 0
        self.physical_position = 0
        self.pending: list[
            tuple[ExtractedLine | ExtractedBlock, PhysicalSourceRef, int]
        ] = []
        self.explicit: dict[
            PhysicalSourceRef, tuple[ParseEntity, tuple[PhysicalSourceRef, ...], int]
        ] = {}
        if isinstance(self.plan, LogParsePlan | DocumentParsePlan):
            self.scope = (
                self.plan.line_refs
                if isinstance(self.plan, LogParsePlan)
                else self.plan.block_refs
            )
            self.scope_set = set(self.scope)
            for entity in self.plan.entities:
                if isinstance(entity.grouping, ExplicitRecordGrouping):
                    for record in entity.grouping.records:
                        for index, ref in enumerate(record):
                            self.explicit[ref] = (entity, record, index)
        else:
            self.scope = ()
            self.scope_set = set()

    def ref(self, kind: PhysicalObjectKind, local_id: str) -> PhysicalSourceRef:
        return PhysicalSourceRef(
            extraction_id=self.request.manifest.extraction_id,
            batch_index=self.batch_index,
            kind=kind,
            local_id=local_id,
        )

    def _observe(self, field: ParseField, ref: PhysicalSourceRef) -> None:
        if ref in field.source_refs:
            self.field_seen[field.field_id].add(ref)

    def consume(self, batch: ExtractedBatch) -> Iterator[SelectedRecord]:
        try:
            checked = self.check.accept(batch)
        except SecurityPolicyError:
            raise failure(
                "physical_source_limit",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            ) from None
        except StructuralProfilingError:
            raise failure(
                "physical_stream_integrity",
                code="PARSE_PLAN_SOURCE_MISMATCH",
                stage=ExecutionStage.SOURCE,
            ) from None
        self.batch_index = checked.batch_index
        units = (
            len(checked.lines)
            + len(checked.blocks)
            + sum(node.segment_index in {None, 0} for node in checked.trees)
        )
        for table in checked.tables:
            if any(
                cell.column_index >= self.options.source_limits.max_columns
                for cell in table.cells
            ):
                raise failure(
                    "source_columns_limit",
                    code="SECURITY_LIMIT_EXCEEDED",
                    stage=ExecutionStage.LIMIT,
                )
            start = (
                table.row_start_index
                if table.row_start_index is not None
                else min((c.row_index for c in table.cells), default=0)
            )
            end = (
                table.row_end_index
                if table.row_end_index is not None
                else max((c.row_index for c in table.cells), default=-1)
            )
            units += end - start + 1
        self.total_items += units
        if self.total_items > self.options.source_limits.max_total_items:
            raise failure(
                "source_total_items",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )
        manifest = self.request.manifest
        if (
            checked.batch_index >= len(manifest.batches)
            or checked.to_summary() != manifest.batches[checked.batch_index]
            or (checked.is_last and checked.manifest != manifest)
        ):
            raise failure(
                "source_snapshot_changed",
                code="PARSE_PLAN_SOURCE_MISMATCH",
                stage=ExecutionStage.SOURCE,
                batch_index=self.batch_index,
            )
        self.required.difference_update(checked.physical_refs())
        if isinstance(self.plan, TabularParsePlan):
            selected = self._table(checked)
        elif isinstance(self.plan, TreeParsePlan):
            selected = self._tree(checked)
        elif self.is_span_plan:
            self._collect_spans(checked)
            selected = iter(())
        else:
            selected = self._physical_groups(checked)
        for record in selected:
            self.record_count += 1
            if self.record_count > self.options.max_records:
                raise failure(
                    "records_limit",
                    code="SECURITY_LIMIT_EXCEEDED",
                    stage=ExecutionStage.LIMIT,
                )
            yield record

    def _table(self, batch: ExtractedBatch) -> Iterator[SelectedRecord]:
        plan = self.plan
        assert isinstance(plan, TabularParsePlan)
        assert plan.data_end_row is not None
        for table in batch.tables:
            if table.table_id != plan.table_ref.local_id:
                continue
            cells_by_row: dict[int, dict[int, ExtractedCell]] = defaultdict(dict)
            for physical_cell in table.cells:
                cells_by_row[physical_cell.row_index][physical_cell.column_index] = (
                    physical_cell
                )
            start = (
                table.row_start_index
                if table.row_start_index is not None
                else min(cells_by_row, default=0)
            )
            end = (
                table.row_end_index
                if table.row_end_index is not None
                else max(cells_by_row, default=-1)
            )
            if end - start + 1 > self.options.source_limits.max_total_items:
                raise failure(
                    "table_range_limit",
                    code="SECURITY_LIMIT_EXCEEDED",
                    stage=ExecutionStage.LIMIT,
                )
            for row in range(start, end + 1):
                cells = cells_by_row.get(row, {})
                if row == plan.header_row:
                    self.header = tuple(
                        (col, cell.value.raw_value)
                        for col, cell in sorted(cells.items())
                    )
                if row == plan.footer_start_row:
                    self.footer_seen = True
                selected = plan.data_start_row <= row <= plan.data_end_row
                if selected:
                    self.data_rows += 1
                for field in plan.fields:
                    selector = field.selector
                    assert isinstance(selector, TabularColumnSelector)
                    self._observe(
                        field, self.ref(PhysicalObjectKind.TABLE, table.table_id)
                    )
                    cell = cells.get(selector.column_index)
                    if cell is not None:
                        self._observe(
                            field, self.ref(PhysicalObjectKind.CELL, cell.cell_id)
                        )
                        if cell.value.value_id is not None:
                            self._observe(
                                field,
                                self.ref(PhysicalObjectKind.VALUE, cell.value.value_id),
                            )
                if row in plan.repeated_header_rows:
                    if (
                        self.header is None
                        or tuple(
                            (col, cell.value.raw_value)
                            for col, cell in sorted(cells.items())
                        )
                        != self.header
                    ):
                        raise failure("repeated_header_mismatch")
                    continue
                if not selected:
                    continue
                budget = _RecordBudget(self.options)
                budget.entity()
                values: list[SelectedValue] = []
                for field in plan.fields:
                    selector = field.selector
                    assert isinstance(selector, TabularColumnSelector)
                    cell = cells.get(selector.column_index)
                    if cell is None:
                        raise failure("cell_missing")
                    budget.append(
                        values,
                        copied(
                            field,
                            PhysicalValueOrigin(
                                source_ref=self.ref(
                                    PhysicalObjectKind.CELL, cell.cell_id
                                ),
                                raw_value=cell.value.raw_value,
                                location=cell.value.location,
                            ),
                        ),
                    )
                yield SelectedRecord((SelectedEntity(plan.entities[0], tuple(values)),))

    def _physical_groups(self, batch: ExtractedBatch) -> Iterator[SelectedRecord]:
        plan = self.plan
        assert isinstance(plan, LogParsePlan | DocumentParsePlan)
        objects = batch.lines if isinstance(plan, LogParsePlan) else batch.blocks
        for obj in objects:
            self.physical_position += 1
            ref = (
                self.ref(PhysicalObjectKind.LINE, obj.line_id)
                if isinstance(obj, ExtractedLine)
                else self.ref(PhysicalObjectKind.BLOCK, obj.block_id)
            )
            if ref not in self.scope_set:
                continue
            if (
                self.scope_position >= len(self.scope)
                or self.scope[self.scope_position] != ref
            ):
                raise failure("physical_scope_order")
            self.scope_position += 1
            if self.pending and self.pending[-1][2] + 1 != self.physical_position:
                raise failure("noncontiguous_record")
            if ref in self.explicit:
                entity, refs, offset = self.explicit[ref]
                if offset != len(self.pending) or (
                    self.pending and self.pending[0][1] != refs[0]
                ):
                    raise failure("overlapping_record")
                self.pending.append((obj, ref, self.physical_position))
                self._pending_limit()
                if len(self.pending) == len(refs):
                    yield self._group_record(entity)
                continue
            entity = plan.entities[0]
            grouping = entity.grouping
            if isinstance(grouping, LogLineGrouping):
                if (
                    grouping.strategy == "blank_line"
                    and isinstance(obj, ExtractedLine)
                    and not obj.text.strip()
                ):
                    if self.pending:
                        yield self._group_record(entity)
                    continue
                maximum = (
                    grouping.lines_per_record
                    if grouping.strategy == "fixed_lines"
                    else 1
                    if grouping.strategy == "one_line"
                    else None
                )
            else:
                assert isinstance(grouping, DocumentBlockGrouping)
                maximum = grouping.max_blocks_per_entity
            self.pending.append((obj, ref, self.physical_position))
            self._pending_limit()
            if maximum is not None and len(self.pending) == maximum:
                yield self._group_record(entity)

    def _pending_limit(self) -> None:
        maximum = (
            self.plan.max_lines_per_record
            if isinstance(self.plan, LogParsePlan)
            else 64
        )
        if (
            len(self.pending) > maximum
            or sum(len(obj.canonical_json()) * 4 for obj, _, _ in self.pending)
            > self.options.max_record_bytes
        ):
            raise failure(
                "pending_record_limit",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )

    def _group_record(self, entity: ParseEntity) -> SelectedRecord:
        group = self.pending
        self.pending = []
        budget = _RecordBudget(self.options)
        budget.entity()
        values: list[SelectedValue] = []
        group_refs = {ref for _, ref, _ in group}
        for field in self.plan.fields:
            if field.field_id not in entity.field_ids:
                continue
            self.field_seen[field.field_id].update(
                group_refs.intersection(field.source_refs)
            )
            selector = field.selector
            if isinstance(selector, LogRecordSelector):
                origins = tuple(
                    PhysicalValueOrigin(
                        source_ref=ref,
                        raw_value=StringScalar(value=obj.text or ""),
                        location=obj.location,
                    )
                    for obj, ref, _ in group
                )
                raw = StringScalar(
                    value="\n".join(obj.text or "" for obj, _, _ in group)
                )
                budget.append(
                    values,
                    SelectedValue(
                        field, raw, raw, origins, SelectionOperation.JOIN_LINES
                    ),
                )
                continue
            offset = (
                selector.line_offset
                if isinstance(selector, LogTokenSelector)
                else selector.block_offset
                if isinstance(selector, DocumentTargetSelector)
                else None
            )
            if offset is None or offset >= len(group):
                raise failure("selector_offset")
            obj, ref, _ = group[offset]
            if obj.text is None:
                if (
                    not isinstance(obj, ExtractedBlock)
                    or not obj.lines
                    or len(obj.lines) > 64
                ):
                    raise failure("block_text_missing_or_line_limit")
                origins = tuple(
                    PhysicalValueOrigin(
                        source_ref=PhysicalSourceRef(
                            extraction_id=ref.extraction_id,
                            batch_index=ref.batch_index,
                            kind=PhysicalObjectKind.LINE,
                            local_id=line.line_id,
                        ),
                        raw_value=StringScalar(value=line.text),
                        location=line.location,
                    )
                    for line in obj.lines
                )
                text = "\n".join(line.text for line in obj.lines)
                raw = StringScalar(value=text)
            else:
                text = obj.text
                raw = StringScalar(value=text)
                origins = (
                    PhysicalValueOrigin(
                        source_ref=ref, raw_value=raw, location=obj.location
                    ),
                )
            origin = origins[0]
            if isinstance(selector, LogTokenSelector):
                parts = tokens(text, selector.delimiter, self.options.max_record_items)
                if selector.token_index >= len(parts):
                    raise failure("token_missing")
                value = parts[selector.token_index]
                operation = SelectionOperation.SELECT_TOKEN
            else:
                assert isinstance(selector, DocumentTargetSelector)
                if selector.target == "block_text":
                    if obj.text is not None:
                        budget.append(values, copied(field, origin))
                    else:
                        budget.append(
                            values,
                            SelectedValue(
                                field, raw, raw, origins, SelectionOperation.JOIN_LINES
                            ),
                        )
                    continue
                key, separator, raw_value = text.partition(":")
                if not separator or (
                    selector.key_equals is not None
                    and key.strip() != selector.key_equals
                ):
                    raise failure("document_key_mismatch")
                value = key.strip() if selector.target == "key" else raw_value.strip()
                operation = (
                    SelectionOperation.SELECT_KEY
                    if selector.target == "key"
                    else SelectionOperation.SELECT_VALUE
                )
            if field.semantic_type == "money":
                raise failure("money_conversion_policy_required")
            budget.append(
                values,
                SelectedValue(
                    field,
                    raw,
                    StringScalar(value=value),
                    origins,
                    operation,
                ),
            )
        return SelectedRecord((SelectedEntity(entity, tuple(values)),))

    def _tree(self, batch: ExtractedBatch) -> Iterator[SelectedRecord]:
        plan = self.plan
        assert isinstance(plan, TreeParsePlan)
        nodes = {node.node_id: node for node in batch.trees}
        root = nodes.get(plan.root_ref.local_id)
        if root is None:
            return
        if self.root_seen and root.segment_index in {None, 0}:
            raise failure("tree_root_repeated")
        stamp = (
            root.name,
            root.raw_name,
            str(root.node_kind),
            root.parent_id,
            root.location.canonical_json(),
        )
        if self.root_stamp is not None and self.root_stamp != stamp:
            raise failure("tree_root_changed")
        self.root_stamp = stamp
        self.root_seen = True
        for field in plan.fields:
            self._observe(field, plan.root_ref)
        descendants = []
        for node in batch.trees:
            current = node
            while current.parent_id is not None and current.node_id != root.node_id:
                current = nodes[current.parent_id]
            if current.node_id == root.node_id:
                descendants.append(
                    (node, self.ref(PhysicalObjectKind.TREE_NODE, node.node_id))
                )
        root_entity = next(e for e in plan.entities if e.parent_entity_id is None)
        root_group = root_entity.grouping
        assert isinstance(root_group, TreeNodeGrouping)
        top = record_steps(root_group)
        if not top:
            # Root entity может пересекать physical segments; он остаётся одним
            # bounded record. Огромный root требует более узкого ParsePlan.
            for node, ref in descendants:
                if node.node_id == root.node_id and self.tree_buffer:
                    continue
                self.tree_bytes += len(node.canonical_json().encode("utf-8")) + 256
                if (
                    len(self.tree_buffer) >= self.options.max_record_items
                    or self.tree_bytes > self.options.max_record_bytes
                ):
                    raise failure(
                        "tree_record_budget",
                        code="SECURITY_LIMIT_EXCEEDED",
                        stage=ExecutionStage.LIMIT,
                    )
                if node.node_id in self.tree_buffer_ids:
                    raise failure("duplicate_buffered_tree_node")
                self.tree_buffer_ids.add(node.node_id)
                self.tree_buffer.append((node, ref))
            if root.is_last_segment is False:
                return
            descendants = self.tree_buffer
            self.tree_buffer = []
            self.tree_buffer_ids.clear()
        by_id = {node.node_id: (node, ref) for node, ref in descendants}
        children: dict[str, list[ExtractedTreeNode]] = defaultdict(list)
        for node, _ in descendants:
            if node.parent_id is not None:
                children[node.parent_id].append(node)
        for group in children.values():
            group.sort(key=lambda n: n.order)
        matches, exists = self._walk(
            [root], top, children, legacy=bool(root_group.record_path)
        )
        self.tree_path_seen |= exists
        for match in matches:
            selected: list[SelectedEntity] = []
            self._tree_entity(
                root_entity,
                match,
                None,
                selected,
                by_id,
                children,
                _RecordBudget(self.options),
            )
            yield SelectedRecord(tuple(selected))
        # Только root children пересекают batch boundary; duplicate raw keys
        # сохраняют occurrence, а не выбираются случайным last-write-wins.
        if root.node_kind in {PhysicalNodeKind.OBJECT, PhysicalNodeKind.MAPPING}:
            for child in children.get(root.node_id, ()):
                name = child.raw_name if child.raw_name is not None else child.name
                self.tree_root_counts[name] = self.tree_root_counts.get(name, 0) + 1
        if len(self.tree_root_counts) > self.options.source_limits.max_columns:
            raise failure(
                "root_key_limit",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )

    def _walk(
        self,
        starts: list[ExtractedTreeNode],
        path: tuple[TreeStep, ...],
        children: dict[str, list[ExtractedTreeNode]],
        *,
        legacy: bool = False,
    ) -> tuple[list[ExtractedTreeNode], bool]:
        current = starts
        empty_array = False
        for step in path:
            # Пустая коллекция подтверждает только конечный ITEM. Более глубокие
            # selectors нельзя считать существующими без physical evidence.
            empty_array = False
            following = []
            for parent in current:
                group = children.get(parent.node_id, [])
                if step.operation is TreePathOperation.ITEM:
                    if parent.node_kind not in {
                        PhysicalNodeKind.ARRAY,
                        PhysicalNodeKind.SEQUENCE,
                    }:
                        raise failure("item_requires_array")
                    following.extend(group)
                    empty_array |= not group
                else:
                    matches = [
                        node
                        for node in group
                        if (
                            node.name
                            if legacy
                            else node.raw_name
                            if node.raw_name is not None
                            else node.name
                        )
                        == step.name
                    ]
                    offset = (
                        self.tree_root_counts.get(step.name, 0)
                        if isinstance(self.plan, TreeParsePlan)
                        and parent.node_id == self.plan.root_ref.local_id
                        else 0
                    )
                    occurrence = step.occurrence - offset
                    if legacy and len(matches) > 1:
                        raise failure("ambiguous_legacy_path")
                    if 0 <= occurrence < len(matches):
                        following.append(matches[occurrence])
            current = following
        return current, bool(current) or empty_array

    def _tree_entity(
        self,
        definition: ParseEntity,
        node: ExtractedTreeNode,
        parent: int | None,
        selected: list[SelectedEntity],
        nodes: dict[str, tuple[ExtractedTreeNode, PhysicalSourceRef]],
        children: dict[str, list[ExtractedTreeNode]],
        budget: _RecordBudget,
    ) -> None:
        assert isinstance(self.plan, TreeParsePlan)
        budget.entity()
        values: list[SelectedValue] = []
        for field in self.plan.fields:
            if field.field_id not in definition.field_ids:
                continue
            selector = field.selector
            assert isinstance(selector, TreePathSelector)
            steps = selector.steps or tuple(
                TreeStep(operation=TreePathOperation.KEY, name=name)
                for name in selector.relative_path
            )
            targets, _ = self._walk(
                [node], steps, children, legacy=bool(selector.relative_path)
            )
            if len(targets) != 1:
                raise failure("field_path_cardinality")
            target = targets[0]
            ref = nodes[target.node_id][1]
            self._observe(field, ref)
            self._observe(field, self.plan.root_ref)
            if target.value is not None and target.value.value_id is not None:
                self._observe(
                    field,
                    PhysicalSourceRef(
                        extraction_id=ref.extraction_id,
                        batch_index=ref.batch_index,
                        kind=PhysicalObjectKind.VALUE,
                        local_id=target.value.value_id,
                    ),
                )
            if selector.value_source == "node_name":
                raw = StringScalar(
                    value=target.raw_name
                    if target.raw_name is not None
                    else target.name
                )
                origin = PhysicalValueOrigin(
                    source_ref=ref, raw_value=raw, location=target.location
                )
                budget.append(
                    values,
                    SelectedValue(
                        field, raw, raw, (origin,), SelectionOperation.NODE_NAME
                    ),
                )
            elif target.value is not None:
                budget.append(
                    values,
                    copied(
                        field,
                        PhysicalValueOrigin(
                            source_ref=ref,
                            raw_value=target.value.raw_value,
                            location=target.value.location,
                        ),
                    ),
                )
            else:
                raise failure("node_value_missing")
        index = len(selected)
        selected.append(SelectedEntity(definition, tuple(values), parent))
        grouping = definition.grouping
        assert isinstance(grouping, TreeNodeGrouping)
        for child_definition in self.plan.entities:
            if child_definition.parent_entity_id != definition.entity_id:
                continue
            child_group = child_definition.grouping
            assert isinstance(child_group, TreeNodeGrouping)
            relative = record_steps(child_group)[len(record_steps(grouping)) :]
            matches, exists = self._walk(
                [node], relative, children, legacy=bool(child_group.record_path)
            )
            if not exists:
                raise failure("child_collection_missing")
            for match in matches:
                self._tree_entity(
                    child_definition, match, index, selected, nodes, children, budget
                )

    def _collect_spans(self, batch: ExtractedBatch) -> None:
        for source in text_sources(batch):
            for span in self.span_index.get(source.ref, ()):
                if span.end > len(source.text):
                    raise failure("span_outside_source")
                text = source.text[span.start : span.end]
                if canonical_sha256_value(text) != span.text_fingerprint:
                    raise failure("span_quote_mismatch")
                self.span_bytes += (
                    len(text.encode())
                    + len(source.location.canonical_json().encode())
                    + 256
                )
                if self.span_bytes > self.options.max_output_batch_bytes:
                    raise failure(
                        "span_buffer_limit",
                        code="SECURITY_LIMIT_EXCEEDED",
                        stage=ExecutionStage.LIMIT,
                    )
                self.span_values[span] = (
                    text,
                    PhysicalValueOrigin(
                        source_ref=source.ref,
                        raw_value=StringScalar(value=text),
                        location=source.location,
                        source_spans=(span,),
                    ),
                )
            for field in self.plan.fields:
                selector = field.selector
                assert isinstance(selector, DocumentSpanSelector)
                if any(span.source_ref == source.ref for span in selector.spans):
                    self._observe(field, source.ref)

    def _span_records(self) -> Iterator[SelectedRecord]:
        groups: dict[str, list[ParseEntity]] = {}
        for entity in self.plan.entities:
            assert isinstance(entity.grouping, DocumentSpanGrouping)
            if entity.grouping.anchor not in self.span_values:
                raise failure("span_anchor_missing")
            groups.setdefault(entity.grouping.record_id, []).append(entity)
        for definitions in groups.values():
            selected: list[SelectedEntity] = []
            positions: dict[str, int] = {}
            pending = list(definitions)
            budget = _RecordBudget(self.options)
            while pending:
                ready = next(
                    (
                        e
                        for e in pending
                        if e.parent_entity_id is None or e.parent_entity_id in positions
                    ),
                    None,
                )
                if ready is None:
                    raise failure("span_parent_cycle")
                pending.remove(ready)
                budget.entity()
                values: list[SelectedValue] = []
                for field in self.plan.fields:
                    if field.field_id not in ready.field_ids:
                        continue
                    selector = field.selector
                    assert isinstance(selector, DocumentSpanSelector)
                    if any(span not in self.span_values for span in selector.spans):
                        raise failure("span_source_missing")
                    parts = [self.span_values[span] for span in selector.spans]
                    by_ref: dict[PhysicalSourceRef, list[PhysicalValueOrigin]] = {}
                    for _, origin in parts:
                        by_ref.setdefault(origin.source_ref, []).append(origin)
                    origins = tuple(
                        PhysicalValueOrigin(
                            source_ref=ref,
                            location=items[0].location,
                            raw_value=StringScalar(
                                value=" ".join(
                                    str(item.raw_value.value) for item in items
                                )
                            ),
                            source_spans=tuple(
                                span for item in items for span in item.source_spans
                            ),
                        )
                        for ref, items in by_ref.items()
                    )
                    raw = StringScalar(value=" ".join(text for text, _ in parts))
                    value = SelectedValue(
                        field, raw, raw, origins, SelectionOperation.SELECT_SPANS
                    )
                    budget.append(values, value)
                positions[ready.entity_id] = len(selected)
                selected.append(
                    SelectedEntity(
                        ready,
                        tuple(values),
                        positions.get(ready.parent_entity_id)
                        if ready.parent_entity_id
                        else None,
                    )
                )
            self.record_count += 1
            if self.record_count > self.options.max_records:
                raise failure(
                    "records_limit",
                    code="SECURITY_LIMIT_EXCEEDED",
                    stage=ExecutionStage.LIMIT,
                )
            yield SelectedRecord(tuple(selected))

    def finish(self) -> Iterator[SelectedRecord]:
        try:
            manifest = self.check.finish()
        except StructuralProfilingError:
            raise failure(
                "terminal_source_missing",
                code="PARSE_EXECUTION_SOURCE_ERROR",
                stage=ExecutionStage.SOURCE,
            ) from None
        if manifest != self.request.manifest:
            raise failure(
                "terminal_manifest_changed",
                code="PARSE_PLAN_SOURCE_MISMATCH",
                stage=ExecutionStage.SOURCE,
            )
        if self.required:
            raise failure("source_reference_missing")
        plan = self.plan
        if isinstance(plan, TabularParsePlan):
            assert plan.data_end_row is not None
            if (
                self.header is None
                or self.data_rows != plan.data_end_row - plan.data_start_row + 1
                or (plan.footer_start_row is not None and not self.footer_seen)
            ):
                raise failure("row_range_missing")
        elif isinstance(plan, TreeParsePlan):
            if not self.root_seen or not self.tree_path_seen or self.tree_buffer:
                raise failure("record_path_missing_or_incomplete")
        elif self.is_span_plan:
            yield from self._span_records()
        else:
            if self.scope_position != len(self.scope):
                raise failure("physical_scope_missing")
            if self.pending:
                grouping = plan.entities[0].grouping
                if (
                    not isinstance(grouping, LogLineGrouping)
                    or grouping.strategy != "blank_line"
                ):
                    raise failure("incomplete_record")
                record = self._group_record(plan.entities[0])
                self.record_count += 1
                if self.record_count > self.options.max_records:
                    raise failure(
                        "records_limit",
                        code="SECURITY_LIMIT_EXCEEDED",
                        stage=ExecutionStage.LIMIT,
                    )
                yield record
        for field in plan.fields:
            if not set(field.source_refs) <= self.field_seen[field.field_id]:
                raise failure("field_evidence_outside_scope")
