"""Ограниченная перепроверка physical batches до structural sampling."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import PhysicalSourceRef
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedDatasetManifest,
)
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.exceptions import SecurityPolicyError, StructuralProfilingError


def invalid(reason: str) -> StructuralProfilingError:
    return StructuralProfilingError(
        error_code="STRUCTURE_INPUT_INVALID",
        message="Некорректный physical stream для profiler",
        details={"reason": reason},
    )


def limit(resource: str, maximum: int) -> SecurityPolicyError:
    return SecurityPolicyError(
        error_code="SECURITY_LIMIT_EXCEEDED",
        message="Превышен ресурсный бюджет structural profiler",
        details={"resource": resource, "limit": maximum},
    )


def preflight(batch: object, options: StructuralProfilingOptions) -> None:
    """Оценить размер DTO до deep validation/serialization, включая forged DTO."""
    stack: list[tuple[object, int]] = [(batch, 0)]
    count = 0
    size = 0
    while stack:
        value, depth = stack.pop()
        count += 1
        size += 32
        if count > options.max_batch_items:
            raise limit("profile_batch_items", options.max_batch_items)
        if depth > 64:
            raise invalid("physical_object_depth")
        if isinstance(value, str | bytes):
            size += len(value) * 4
        elif isinstance(value, tuple | list):
            if len(value) + len(stack) + count > options.max_batch_items:
                raise limit("profile_batch_items", options.max_batch_items)
            stack.extend((child, depth + 1) for child in value)
        elif isinstance(value, BaseModel):
            stack.extend(
                (getattr(value, name), depth + 1) for name in type(value).model_fields
            )
        elif isinstance(value, int):
            size += value.bit_length() // 8
        elif isinstance(value, Decimal):
            size += len(value.as_tuple().digits)
        elif value is not None and not isinstance(value, float | date | datetime):
            raise invalid("physical_value_type")
        if size > options.max_batch_bytes:
            raise limit("profile_batch_bytes", options.max_batch_bytes)


class StreamCheck:
    """Хранит только ограниченные summaries, refs и continuation counters."""

    def __init__(self, options: StructuralProfilingOptions) -> None:
        self.options = options
        self.summaries: list[ExtractedBatchSummary] = []
        self.index: list[PhysicalSourceRef] = []
        self.known_refs: set[PhysicalSourceRef] = set()
        self.tables: dict[str, tuple[int, int, bool]] = {}
        self.trees: dict[str, tuple[int, int, bool, str, str]] = {}
        self.manifest: ExtractedDatasetManifest | None = None
        self.schema_version: str | None = None
        self.physical_count = 0

    def accept(self, batch: ExtractedBatch) -> ExtractedBatch:
        if type(batch) is not ExtractedBatch:
            raise invalid("batch_type")
        if self.manifest is not None:
            raise invalid("batch_after_terminal")
        if len(self.summaries) >= self.options.max_batches:
            raise limit("profile_batches", self.options.max_batches)
        try:
            preflight(batch, self.options)
            checked = ExtractedBatch.model_validate(
                batch.model_dump(mode="python", warnings="error")
            )
            self._accept_checked(checked)
        except (ValueError, TypeError, AttributeError):
            raise invalid("physical_contract") from None
        return checked

    def _accept_checked(self, batch: ExtractedBatch) -> None:
        if (
            self.schema_version is not None
            and batch.schema_version != self.schema_version
        ):
            raise invalid("batch_schema_changed")
        self.schema_version = batch.schema_version
        if batch.batch_index != len(self.summaries):
            raise invalid("batch_sequence")
        summary = batch.to_summary()
        self.physical_count += summary.physical_ref_count
        if self.physical_count > self.options.max_physical_objects:
            raise limit("profile_physical_objects", self.options.max_physical_objects)
        if self.summaries:
            first = self.summaries[0]
            if (
                summary.source,
                summary.extraction_id,
                summary.parser_id,
                summary.parser_version,
            ) != (
                first.source,
                first.extraction_id,
                first.parser_id,
                first.parser_version,
            ):
                raise invalid("source_lineage")
        if batch.schema_version == "1.1.0":
            expected = canonical_sha256_value(
                batch, exclude_top_level=frozenset({"batch_fingerprint", "manifest"})
            )
            if expected != batch.batch_fingerprint:
                raise invalid("batch_fingerprint")
            refs = batch.indexed_refs
        else:
            # Legacy extraction не публикует progressive index. Его bounded
            # catalog проверяется целиком до принятия terminal manifest.
            refs = batch.physical_refs()
        if len(self.index) + len(refs) > 10_000:
            raise limit("profile_indexed_refs", 10_000)
        if any(ref in self.known_refs for ref in refs):
            raise invalid("duplicate_index_ref")
        self.index.extend(refs)
        self.known_refs.update(refs)
        self.summaries.append(summary)
        self._tree_depths(batch)
        self._continuations(batch)
        if batch.manifest is not None:
            manifest = batch.manifest
            if manifest.batches != tuple(self.summaries):
                raise invalid("manifest_summaries")
            if manifest.schema_version != batch.schema_version:
                raise invalid("manifest_schema")
            if batch.schema_version == "1.1.0":
                if manifest.source_index.refs != tuple(self.index):
                    raise invalid("manifest_index")
                if (
                    canonical_sha256_value(
                        manifest,
                        exclude_top_level=frozenset({"extraction_fingerprint"}),
                    )
                    != manifest.extraction_fingerprint
                ):
                    raise invalid("manifest_fingerprint")
            elif not set(manifest.source_index.refs) <= self.known_refs:
                raise invalid("manifest_index")
            if any(not item[2] for item in self.tables.values()) or any(
                not item[2] for item in self.trees.values()
            ):
                raise invalid("unfinished_segment")
            self.manifest = manifest

    def _tree_depths(self, batch: ExtractedBatch) -> None:
        parents = {node.node_id: node.parent_id for node in batch.trees}
        depths: dict[str, int] = {}
        for node_id in parents:
            pending: list[str] = []
            active: set[str] = set()
            current: str | None = node_id
            while current is not None and current not in depths:
                if current in active or current not in parents:
                    raise invalid("tree_graph")
                if len(pending) > self.options.max_depth:
                    raise limit("profile_tree_depth", self.options.max_depth)
                active.add(current)
                pending.append(current)
                current = parents[current]
            depth = depths[current] if current is not None else -1
            for ancestor in reversed(pending):
                depth += 1
                if depth > self.options.max_depth:
                    raise limit("profile_tree_depth", self.options.max_depth)
                depths[ancestor] = depth

    def _continuations(self, batch: ExtractedBatch) -> None:
        for table in batch.tables:
            if table.segment_index is None:
                continue
            start, end, last = (
                table.row_start_index,
                table.row_end_index,
                table.is_last_segment,
            )
            if start is None or end is None or last is None:
                raise invalid("table_segment")
            previous = self.tables.get(table.table_id)
            if previous is None:
                if len(self.tables) >= self.options.max_structures:
                    raise limit("profile_tables", self.options.max_structures)
                if table.segment_index != 0:
                    raise invalid("table_segment_start")
            elif previous != (table.segment_index, start, False):
                raise invalid("table_continuity")
            self.tables[table.table_id] = (table.segment_index + 1, end + 1, last)
        for node in batch.trees:
            if node.tree_id is None:
                continue
            segment, start, count, last = (
                node.segment_index,
                node.child_start_index,
                node.child_count,
                node.is_last_segment,
            )
            if segment is None or start is None or count is None or last is None:
                raise invalid("tree_segment")
            previous_tree = self.trees.get(node.tree_id)
            if previous_tree is None:
                if len(self.trees) >= self.options.max_structures:
                    raise limit("profile_trees", self.options.max_structures)
                if segment != 0 or start != 0:
                    raise invalid("tree_segment_start")
            elif previous_tree != (
                segment,
                start,
                False,
                str(node.node_kind),
                node.node_id,
            ):
                raise invalid("tree_continuity")
            self.trees[node.tree_id] = (
                segment + 1,
                start + count,
                last,
                str(node.node_kind),
                node.node_id,
            )

    def finish(self) -> ExtractedDatasetManifest:
        if self.manifest is None:
            raise invalid("terminal_manifest_missing")
        return self.manifest
