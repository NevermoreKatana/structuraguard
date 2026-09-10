"""Закрытая компиляция подтверждённых физических гипотез, без execution."""

from collections import Counter
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.analysis import (
    AnalysisScore,
    ExplicitRecordGrouping,
    LogRecordSelector,
    PlanDerivation,
    StructureAnalysisOptions,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.common import (
    ParsePlanKind,
    PhysicalSourceRef,
    ProducerMetadata,
)
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    DocumentTargetSelector,
    LogParsePlan,
    ParseEntity,
    ParseField,
    ParsePlan,
    StructureCandidate,
    TabularColumnSelector,
    TabularParsePlan,
    TabularRowGrouping,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
    _ParsePlanBase,
)
from structuraguard.contracts.source import ExtractedBlockKind, PhysicalNodeKind
from structuraguard.contracts.structure import TabularObservation
from structuraguard.structure._observations import field_name, ratio
from structuraguard.structure._samples import Block, Line, Node, Row
from structuraguard.structure.profiling import ProfiledSource
from structuraguard.structure.text import signatures


@dataclass(frozen=True, slots=True)
class Draft:
    candidate: StructureCandidate
    plan: ParsePlan | None


class Planner:
    """Локальный run state; bounds наследуются от проверенного snapshot."""

    def __init__(
        self, snapshot: ProfiledSource, options: StructureAnalysisOptions
    ) -> None:
        self.snapshot = snapshot
        self.options = options
        self.drafts: list[Draft] = []
        self.overflow = False
        self.unhandled = False

    def candidate(
        self,
        kind: ParsePlanKind,
        rule: str,
        refs: tuple[PhysicalSourceRef, ...],
        boundary: Decimal,
        regularity: Decimal,
        *,
        blockers: tuple[str, ...] = (),
        discriminator: str = "",
    ) -> StructureCandidate | None:
        if len(self.drafts) >= self.options.profiling.max_candidates:
            self.overflow = True
            return None
        profile = self.snapshot.profile
        selected_refs = set(refs)
        observations = tuple(
            o for o in profile.observations if selected_refs.intersection(o.source_refs)
        )[:64]
        if not observations:
            return None
        evidence = tuple(
            dict.fromkeys(ref for item in observations for ref in item.source_refs)
        )
        ids = tuple(o.evidence_id for o in observations)
        coverage = profile.coverage
        score = AnalysisScore(
            options_fingerprint=canonical_sha256_value(self.options),
            boundary=boundary,
            regularity=regularity,
            coverage=ratio(coverage.sampled_items, coverage.seen_items)
            if coverage
            else Decimal(0),
            observation_ids=ids,
            blockers=tuple(
                dict.fromkeys(
                    (
                        *blockers,
                        *(
                            ("incomplete_coverage",)
                            if not coverage or not coverage.complete
                            else ()
                        ),
                    )
                )
            ),
        )
        return StructureCandidate(
            candidate_id="candidate_"
            + canonical_sha256_value(
                (rule, discriminator, tuple(ref.canonical_json() for ref in refs))
            )[-24:],
            source=profile.source,
            extraction_fingerprint=profile.extraction_fingerprint,
            plan_kind=kind,
            confidence=score.confidence,
            evidence=evidence,
            rationale_codes=(rule,),
            observation_ids=ids,
            assessment=score,
        )

    def base(
        self,
        candidate: StructureCandidate,
        fields: tuple[ParseField, ...],
        entities: tuple[ParseEntity, ...],
    ) -> _ParsePlanBase:
        profile = self.snapshot.profile
        assert candidate.assessment is not None
        return _ParsePlanBase(
            plan_id="plan_" + candidate.candidate_id.removeprefix("candidate_"),
            schema_version="1.1.0",
            revision=1,
            source_fingerprint=profile.source.source_fingerprint,
            extraction_fingerprint=profile.extraction_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            confidence=candidate.confidence,
            producer=ProducerMetadata(
                component_id="deterministic_structure_analyzer",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            fields=fields,
            entities=entities,
            evidence=candidate.evidence,
            analysis=PlanDerivation(
                rule=candidate.rationale_codes[0],
                options_fingerprint=canonical_sha256_value(self.options),
                input_profile_fingerprint=profile.profile_fingerprint,
                candidate_id=candidate.candidate_id,
                score=candidate.assessment,
                unresolved_fields=tuple(f.field_id for f in fields),
            ),
        )

    def blocked(self, candidate: StructureCandidate) -> bool:
        if candidate.assessment is not None and candidate.assessment.blockers:
            self.drafts.append(Draft(candidate, None))
            return True
        return False

    def build(self) -> tuple[Draft, ...]:
        for family in (self.tables, self.trees, self.logs, self.documents):
            family()
        return tuple(
            sorted(
                self.drafts,
                key=lambda d: (
                    d.candidate.confidence.copy_negate(),
                    d.candidate.plan_kind,
                    d.candidate.candidate_id,
                ),
            )
        )

    def tables(self) -> None:
        for table_id, table in self.snapshot.samples.tables.items():
            before = len(self.drafts)
            observations = [
                o
                for e in self.snapshot.profile.observations
                if isinstance(o := e.observation, TabularObservation)
                and o.table_id == table_id
            ]
            rows = {row.index: row for row in table.rows}
            headers: set[tuple[tuple[int, str], ...]] = set()
            for observation in observations:
                if observation.role != "header" or observation.row_start not in rows:
                    continue
                header = rows[observation.row_start]
                signature = tuple((i, value) for i, value, _ in header.cells)
                if signature in headers:
                    continue
                headers.add(signature)
                following = [row for row in table.rows if row.index > header.index]
                if not following:
                    continue
                repeated = tuple(
                    row.index
                    for row in following
                    if tuple((i, value) for i, value, _ in row.cells) == signature
                )
                footer = next(
                    (o.row_start for o in observations if o.role == "footer"), None
                )
                data = [
                    r
                    for r in following
                    if r.index not in repeated and (footer is None or r.index < footer)
                ]
                if not data:
                    continue
                columns = tuple(i for i, _, _ in header.cells)
                regularity = ratio(
                    sum(tuple(i for i, _, _ in r.cells) == columns for r in data),
                    len(data),
                )
                contrast = any(
                    hint not in {"string", "null", "identifier"}
                    for row in data
                    for _, _, hint in row.cells
                )
                blockers: list[str] = []
                if regularity != 1:
                    blockers.append("ragged_data")
                if any(
                    not r.cells or all(not v.strip() for _, v, _ in r.cells)
                    for r in data
                ):
                    blockers.append("empty_data_region")
                if any(
                    o.role in {"summary", "merged"} and o.row_start > header.index
                    for o in observations
                ):
                    blockers.append("ambiguous_region")
                if footer is not None and not _verified_total(rows[footer], data):
                    blockers.append("unconfirmed_footer")
                candidate = self.candidate(
                    ParsePlanKind.TABULAR,
                    "tabular_header_contrast_v1",
                    (table.reference,),
                    Decimal("0.9") if contrast else Decimal("0.55"),
                    regularity,
                    blockers=tuple(blockers),
                    discriminator=str(header.index),
                )
                if candidate is None or self.blocked(candidate):
                    continue
                fields = tuple(
                    ParseField(
                        field_id=f"column_{i}",
                        semantic_name=f"{field_name(label, 'column')}_{i}",
                        semantic_type="unresolved",
                        source_refs=(table.reference,),
                        selector=TabularColumnSelector(column_index=i),
                    )
                    for i, label, _ in header.cells
                )
                entity = ParseEntity(
                    entity_id="records",
                    entity_type="unresolved",
                    field_ids=tuple(f.field_id for f in fields),
                    grouping=TabularRowGrouping(),
                )
                base = self.base(candidate, fields, (entity,))
                plan = TabularParsePlan(
                    **base.model_dump(exclude={"fingerprint"}),
                    table_ref=table.reference,
                    header_row=header.index,
                    data_start_row=data[0].index,
                    data_end_row=data[-1].index,
                    footer_start_row=footer,
                    repeated_header_rows=tuple(
                        i for i in repeated if i <= data[-1].index
                    ),
                )
                self.drafts.append(Draft(candidate, plan))
            self.unhandled |= len(self.drafts) == before

    def trees(self) -> None:
        nodes = self.snapshot.samples.nodes
        if not nodes:
            return
        # XML element matching требует собственной namespace/cardinality policy.
        if any(n.kind is PhysicalNodeKind.ELEMENT for n in nodes):
            self.unhandled = True
            return
        _paths, groups = _tree_paths(nodes)
        by_id = {n.reference.local_id: n for n in nodes}
        multiple_roots = sum(n.parent is None for n in nodes) != 1
        record_paths = [
            p
            for p, group in groups.items()
            if p
            and p[-1].operation is TreePathOperation.ITEM
            and any(
                n.kind in {PhysicalNodeKind.OBJECT, PhysicalNodeKind.MAPPING}
                or n.hint is not None
                for n in group
            )
        ]
        has_root_object = () in groups and any(
            n.kind in {PhysicalNodeKind.OBJECT, PhysicalNodeKind.MAPPING}
            for n in groups[()]
        )
        has_outer_fields = any(
            all(n.hint is not None for n in group)
            and all(step.operation is TreePathOperation.KEY for step in path)
            for path, group in groups.items()
        )
        if has_root_object and (not record_paths or has_outer_fields):
            record_paths.insert(0, ())
        if not record_paths:
            self.unhandled = True
            return
        top_paths = [
            p
            for p in record_paths
            if not any(
                p[: len(other)] == other and len(other) < len(p)
                for other in record_paths
            )
        ]
        for top in sorted(top_paths, key=_path_key):
            entity_paths = sorted(
                (p for p in record_paths if p[: len(top)] == top),
                key=lambda p: (len(p), _path_key(p)),
            )
            refs = tuple(
                n.reference
                for p, group in groups.items()
                if p[: len(top)] == top
                for n in group
            )
            blockers: tuple[str, ...] = (
                ("entity_limit",) if len(entity_paths) > 32 else ()
            )
            if multiple_roots:
                blockers += ("multiple_physical_roots",)
            if sum(step.operation is TreePathOperation.ITEM for step in top) > 1:
                blockers += ("array_container_policy_required",)
            if any(
                len(group) != len(groups[path])
                for path in entity_paths
                for field_path, group in groups.items()
                if field_path[: len(path)] == path
                and all(n.hint is not None for n in group)
                and not any(
                    step.operation is TreePathOperation.ITEM
                    for step in field_path[len(path) :]
                )
            ):
                blockers += ("optional_field_policy_required",)
            # Один raw key path обязан иметь один физический тип во всех records.
            if any(
                len({n.kind for n in group}) > 1
                for p, group in groups.items()
                if p[: len(top)] == top
            ):
                blockers += ("heterogeneous_tree",)
            candidate = self.candidate(
                ParsePlanKind.TREE,
                "tree_literal_collections_v1",
                refs,
                Decimal("0.95")
                if any(len(groups[path]) > 1 for path in entity_paths)
                else Decimal("0.8"),
                Decimal(1),
                blockers=blockers,
                discriminator=_path_key(top),
            )
            if candidate is None or self.blocked(candidate):
                continue
            fields: list[ParseField] = []
            entities: list[ParseEntity] = []
            entity_ids = {p: f"records_{i}" for i, p in enumerate(entity_paths)}
            for path in entity_paths:
                child_paths = [
                    p for p in entity_paths if p != path and p[: len(path)] == path
                ]
                field_paths = sorted(
                    (
                        p
                        for p, group in groups.items()
                        if p[: len(path)] == path
                        and all(n.hint is not None for n in group)
                        and not any(p[: len(child)] == child for child in child_paths)
                    ),
                    key=_path_key,
                )
                own_fields: list[ParseField] = []
                for field_path in field_paths:
                    # Array между record и field без child entity не flatten.
                    relative = field_path[len(path) :]
                    if any(
                        step.operation is TreePathOperation.ITEM for step in relative
                    ):
                        continue
                    identifier = f"field_{len(fields) + len(own_fields)}"
                    own_fields.append(
                        ParseField(
                            field_id=identifier,
                            semantic_name=identifier,
                            semantic_type="unresolved",
                            source_refs=tuple(
                                n.reference for n in groups[field_path][:4]
                            ),
                            selector=TreePathSelector(steps=relative),
                        )
                    )
                if not own_fields:
                    break
                parents = [
                    p
                    for p in entity_paths
                    if len(p) < len(path) and path[: len(p)] == p
                ]
                parent = max(parents, key=len) if parents else None
                entities.append(
                    ParseEntity(
                        entity_id=entity_ids[path],
                        entity_type="unresolved",
                        field_ids=tuple(f.field_id for f in own_fields),
                        grouping=TreeNodeGrouping(record_steps=path),
                        parent_entity_id=entity_ids[parent]
                        if parent is not None
                        else None,
                    )
                )
                fields.extend(own_fields)
            if len(entities) != len(entity_paths) or len(fields) > 1_024:
                self.drafts.append(Draft(candidate, None))
                continue
            root = groups[top][0]
            while root.parent is not None:
                root = by_id[root.parent]
            base = self.base(candidate, tuple(fields), tuple(entities))
            self.drafts.append(
                Draft(
                    candidate,
                    TreeParsePlan(
                        **base.model_dump(exclude={"fingerprint"}),
                        root_ref=root.reference,
                        record_steps=top,
                    ),
                )
            )

    def logs(self) -> None:
        lines = [line for line in self.snapshot.samples.lines if not line.block]
        if not lines:
            return
        records: list[list[Line]] = []
        orphan = False
        for line in lines:
            continuation = not line.text.strip() or line.text[:1].isspace()
            if continuation and records and records[-1][-1].end + 1 == line.start:
                records[-1].append(line)
            else:
                orphan |= continuation
                records.append([line])
        clusters: dict[tuple[tuple[str, ...], ...], list[list[Line]]] = {}
        for record in records:
            shapes = tuple(signatures(line.text)[1] for line in record)
            clusters.setdefault(shapes, []).append(record)
        for signature, group in clusters.items():
            start = group[0][0]
            _, shape, timestamp, level = signatures(start.text)
            if not (
                timestamp
                or level
                or any(s in {"integer", "decimal", "key_value"} for s in shape)
            ):
                self.unhandled = True
                continue
            refs = tuple(line.reference for record in group for line in record)
            blockers = tuple(
                code
                for code, applies in (
                    ("orphan_continuation", orphan),
                    ("record_limit", any(len(r) > 64 for r in group)),
                )
                if applies
            )
            candidate = self.candidate(
                ParsePlanKind.LOG,
                "log_physical_records_v1",
                refs,
                Decimal("0.9") if len(group) > 1 else Decimal("0.7"),
                Decimal(1),
                blockers=blockers,
                discriminator=canonical_sha256_value(signature),
            )
            if candidate is None:
                self.unhandled = True
                continue
            if self.blocked(candidate):
                continue
            field = ParseField(
                field_id="raw_record",
                semantic_name="raw_record",
                semantic_type="unresolved",
                source_refs=refs[:4],
                selector=LogRecordSelector(),
            )
            grouping = ExplicitRecordGrouping(
                records=tuple(
                    tuple(line.reference for line in record) for record in group
                )
            )
            entity = ParseEntity(
                entity_id="records",
                entity_type="unresolved",
                field_ids=(field.field_id,),
                grouping=grouping,
            )
            base = self.base(candidate, (field,), (entity,))
            self.drafts.append(
                Draft(
                    candidate,
                    LogParsePlan(
                        **base.model_dump(exclude={"fingerprint"}),
                        line_refs=refs,
                        max_lines_per_record=max(map(len, group)),
                    ),
                )
            )

    def documents(self) -> None:
        duplicates = _duplicated_line_blocks(self.snapshot.samples.lines)
        blocks = [
            b
            for b in self.snapshot.samples.blocks
            if b.kind is not ExtractedBlockKind.METADATA
            and b.reference not in duplicates
        ]
        if not blocks:
            return
        has_headings = any(b.kind is ExtractedBlockKind.HEADING for b in blocks)
        groups: list[list[Block]] = []
        for block in blocks:
            if (
                not has_headings
                or block.kind is ExtractedBlockKind.HEADING
                or not groups
            ):
                groups.append([block])
            else:
                groups[-1].append(block)
        clusters: dict[tuple[ExtractedBlockKind, ...], list[list[Block]]] = {}
        for group in groups:
            clusters.setdefault(tuple(b.kind for b in group), []).append(group)
        for signature, records in clusters.items():
            refs = tuple(b.reference for group in records for b in group)
            candidate = self.candidate(
                ParsePlanKind.DOCUMENT,
                "document_sections_v1"
                if has_headings
                else "document_physical_blocks_v1",
                refs,
                Decimal("0.9") if len(records) > 1 else Decimal("0.7"),
                Decimal(1),
                blockers=("record_limit",) if len(signature) > 64 else (),
                discriminator=canonical_sha256_value(signature),
            )
            if candidate is None or self.blocked(candidate):
                continue
            fields = tuple(
                ParseField(
                    field_id=f"block_{i}",
                    semantic_name=f"block_{i}",
                    semantic_type="unresolved",
                    source_refs=tuple(group[i].reference for group in records[:4]),
                    selector=DocumentTargetSelector(
                        target="block_text", block_offset=i
                    ),
                )
                for i in range(len(signature))
            )
            entity = ParseEntity(
                entity_id="records",
                entity_type="unresolved",
                field_ids=tuple(f.field_id for f in fields),
                grouping=ExplicitRecordGrouping(
                    records=tuple(
                        tuple(b.reference for b in group) for group in records
                    )
                ),
            )
            base = self.base(candidate, fields, (entity,))
            self.drafts.append(
                Draft(
                    candidate,
                    DocumentParsePlan(
                        **base.model_dump(exclude={"fingerprint"}), block_refs=refs
                    ),
                )
            )


def _duplicated_line_blocks(lines: list[Line]) -> set[PhysicalSourceRef]:
    # Исключаем только подтверждённые дубликаты: одного наличия lines
    # недостаточно, чтобы отбросить независимые document blocks.
    physical = {line.start: line for line in lines if not line.block}
    duplicates: set[PhysicalSourceRef] = set()
    for block in lines:
        if not block.block or block.end - block.start + 1 > len(physical):
            continue
        parts: list[str] = []
        for number in range(block.start, block.end + 1):
            line = physical.get(number)
            if line is None or line.ending is None:
                break
            parts.append(line.text + line.ending)
        else:
            if "".join(parts) == block.text:
                duplicates.add(block.reference)
    return duplicates


def _verified_total(footer: Row, data: list[Row]) -> bool:
    verified = False
    with localcontext(Context(prec=4_096)):
        for column, value, hint in footer.cells[1:]:
            if hint not in {"integer", "decimal"}:
                continue
            values = [
                v
                for row in data
                for i, v, h in row.cells
                if i == column and h in {"integer", "decimal"}
            ]
            if len(values) != len(data) or sum(
                (Decimal(v) for v in values), Decimal(0)
            ) != Decimal(value):
                return False
            verified = True
    return verified


def _tree_paths(
    nodes: list[Node],
) -> tuple[dict[str, tuple[TreeStep, ...]], dict[tuple[TreeStep, ...], list[Node]]]:
    by_id = {n.reference.local_id: n for n in nodes}
    occurrences: Counter[tuple[str | None, str]] = Counter()
    steps: dict[str, TreeStep] = {}
    for node in nodes:
        parent = by_id.get(node.parent or "")
        key = (node.parent, node.name)
        item = parent is not None and parent.kind in {
            PhysicalNodeKind.ARRAY,
            PhysicalNodeKind.SEQUENCE,
        }
        steps[node.reference.local_id] = TreeStep(
            operation=TreePathOperation.ITEM if item else TreePathOperation.KEY,
            name="" if item else node.name,
            occurrence=0 if item else occurrences[key],
        )
        occurrences[key] += 1
    paths: dict[str, tuple[TreeStep, ...]] = {}
    groups: dict[tuple[TreeStep, ...], list[Node]] = {}
    for node in nodes:
        current = node
        reverse: list[TreeStep] = []
        while current.parent is not None and len(reverse) <= 30:
            reverse.append(steps[current.reference.local_id])
            if current.parent not in by_id:
                break
            current = by_id[current.parent]
        else:
            path = tuple(reversed(reverse))
            paths[node.reference.local_id] = path
            groups.setdefault(path, []).append(node)
    return paths, groups


def _path_key(path: tuple[TreeStep, ...]) -> str:
    return canonical_sha256_value(tuple(step.canonical_json() for step in path))
