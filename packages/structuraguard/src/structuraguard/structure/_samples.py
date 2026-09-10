"""Bounded physical summaries; ни один batch не сохраняется после consume."""

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import date, datetime

from structuraguard.contracts.common import (
    PhysicalObjectKind,
    PhysicalSourceRef,
    RawScalar,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBlockKind,
    LineRangeLocation,
    PhysicalNodeKind,
    SheetCellLocation,
)
from structuraguard.contracts.structure import PrimitiveHint, StructuralProfilingOptions
from structuraguard.structure._stream import limit


def primitive(text: str) -> PrimitiveHint:
    value = text.strip()
    if not value:
        return "string"
    if value.casefold() in {"true", "false"}:
        return "boolean"
    digits = value[1:] if value.startswith(("-", "+")) else value
    if digits.isascii() and digits.isdigit():
        return "identifier" if len(digits) > 1 and digits[0] == "0" else "integer"
    if len(digits.split(".")) == 2 and all(
        part.isascii() and part.isdigit() for part in digits.split(".")
    ):
        return "decimal"
    if len(value) == 10 and value[4:5] == value[7:8] == "-":
        try:
            date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return "date"
    if 19 <= len(value) <= 40 and value[4:5] == value[7:8] == "-":
        try:
            datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return "timestamp"
    return "string"


def scalar(value: RawScalar, max_chars: int) -> tuple[str, PrimitiveHint] | None:
    raw = value.value
    if isinstance(raw, str):
        return (raw, primitive(raw)) if len(raw) <= max_chars else None
    if raw is None:
        return "", "null"
    if isinstance(raw, bytes):
        return ("", "bytes") if len(raw) <= max_chars else None
    if isinstance(raw, int) and raw.bit_length() > max_chars * 3:
        return None
    text = str(raw)
    if len(text) > max_chars:
        return None
    kind: PrimitiveHint = "decimal" if value.kind == "number" else primitive(text)
    return text, kind


@dataclass(frozen=True, slots=True)
class Row:
    index: int
    cells: tuple[tuple[int, str, PrimitiveHint], ...]


@dataclass(slots=True)
class Table:
    reference: PhysicalSourceRef
    sheet: str | None
    rows: list[Row] = field(default_factory=list)
    widths: Counter[int] = field(default_factory=Counter)
    sparse_widths: Counter[int] = field(default_factory=Counter)
    tail: deque[tuple[Row, int]] = field(default_factory=deque)
    seen: int = 0
    start: int = 0
    end: int = 0


@dataclass(frozen=True, slots=True)
class Node:
    reference: PhysicalSourceRef
    parent: str | None
    name: str
    kind: PhysicalNodeKind | None
    order: int
    hint: PrimitiveHint | None


@dataclass(frozen=True, slots=True)
class Line:
    reference: PhysicalSourceRef
    start: int
    end: int
    text: str
    block: bool = False
    ending: str | None = None


@dataclass(frozen=True, slots=True)
class Block:
    reference: PhysicalSourceRef
    order: int
    kind: ExtractedBlockKind
    text: str
    sheet: str | None
    merged: tuple[str, ...]


class Samples:
    def __init__(self, options: StructuralProfilingOptions) -> None:
        self.options = options
        self.seen = 0
        self.retained = 0
        self.bytes = 0
        self.reasons: set[str] = set()
        self.tables: dict[str, Table] = {}
        self.nodes: list[Node] = []
        self.lines: list[Line] = []
        self.blocks: list[Block] = []

    def seen_item(self) -> None:
        self.seen += 1
        if self.seen > self.options.max_total_items:
            raise limit("profile_total_items", self.options.max_total_items)

    def retain(self, size: int, *, indexed: bool, reserve_tail: bool = False) -> bool:
        if not indexed:
            self.reasons.add("unindexed_source")
            return False
        if size < 0:
            self.reasons.add("oversized_sample")
            return False
        reserve = (
            min(self.options.tail_rows, self.options.max_sample_items // 4)
            if reserve_tail
            else 0
        )
        if self.retained >= self.options.max_sample_items - reserve:
            self.reasons.add("sample_items")
            return False
        byte_limit = (
            self.options.max_sample_bytes * 3 // 4
            if reserve
            else self.options.max_sample_bytes
        )
        if self.bytes + size > byte_limit:
            self.reasons.add("sample_bytes")
            return False
        self.bytes += size
        self.retained += 1
        return True

    def retain_tail(self, table: Table, row: Row, size: int, *, indexed: bool) -> None:
        capacity = min(self.options.tail_rows, self.options.max_sample_items // 4)
        if not capacity or size < 0 or not indexed:
            return
        if len(table.tail) >= capacity:
            _, old_size = table.tail.popleft()
            self.retained -= 1
            self.bytes -= old_size
        if self.retain(size, indexed=indexed):
            table.tail.append((row, size))

    def consume(self, batch: ExtractedBatch, index: set[PhysicalSourceRef]) -> None:
        def ref(kind: PhysicalObjectKind, local_id: str) -> PhysicalSourceRef:
            return PhysicalSourceRef(
                extraction_id=batch.extraction_id,
                batch_index=batch.batch_index,
                kind=kind,
                local_id=local_id,
            )

        for table in batch.tables:
            reference = ref(PhysicalObjectKind.TABLE, table.table_id)
            if table.table_id not in self.tables:
                if len(self.tables) >= self.options.max_structures:
                    raise limit("profile_tables", self.options.max_structures)
                sheet = (
                    table.location.sheet_name
                    if isinstance(table.location, SheetCellLocation)
                    else None
                )
                self.tables[table.table_id] = Table(reference=reference, sheet=sheet)
            state = self.tables[table.table_id]
            # Сортировка ограничена preflight batch budget, не размером source.
            cells = sorted(
                table.cells, key=lambda cell: (cell.row_index, cell.column_index)
            )
            start = (
                table.row_start_index
                if table.row_start_index is not None
                else (cells[0].row_index if cells else 0)
            )
            end = (
                table.row_end_index
                if table.row_end_index is not None
                else (cells[-1].row_index if cells else -1)
            )
            if end - start + 1 > self.options.max_total_items - self.seen:
                raise limit("profile_total_items", self.options.max_total_items)
            offset = 0
            for row_index in range(start, end + 1):
                self.seen_item()
                row_values: list[tuple[int, str, PrimitiveHint]] = []
                size = 128
                width = 0
                present = 0
                while offset < len(cells) and cells[offset].row_index == row_index:
                    cell = cells[offset]
                    offset += 1
                    present += 1
                    width = max(width, cell.column_index + 1)
                    if width > self.options.max_columns:
                        raise limit("profile_columns", self.options.max_columns)
                    value = scalar(cell.value.raw_value, self.options.max_value_chars)
                    if value is None:
                        size = -1
                    elif size >= 0:
                        size += 64 + len(value[0]) * 4
                        row_values.append((cell.column_index, *value))
                if state.seen == 0:
                    state.start = row_index
                state.end = row_index
                state.seen += 1
                state.widths[width] += 1
                if present < width:
                    state.sparse_widths[width] += 1
                row_sample = Row(row_index, tuple(row_values))
                if self.retain(
                    size, indexed=state.reference in index, reserve_tail=True
                ):
                    state.rows.append(row_sample)
                else:
                    self.retain_tail(
                        state, row_sample, size, indexed=state.reference in index
                    )

        for node in batch.trees:
            # Continuation root повторяет тот же physical container.
            if node.segment_index is not None and node.segment_index > 0:
                continue
            self.seen_item()
            reference = ref(PhysicalObjectKind.TREE_NODE, node.node_id)
            name = node.raw_name if node.raw_name is not None else node.name
            value = (
                scalar(node.value.raw_value, self.options.max_value_chars)
                if node.value
                else None
            )
            oversized = len(name) > self.options.max_value_chars or (
                node.value is not None and value is None
            )
            size = (
                -1
                if oversized
                else 256 + len(name) * 4 + (len(value[0]) * 4 if value else 0)
            )
            if self.retain(size, indexed=reference in index):
                self.nodes.append(
                    Node(
                        reference,
                        node.parent_id,
                        name,
                        node.node_kind,
                        node.order,
                        value[1] if value else None,
                    )
                )

        for line in batch.lines:
            self.seen_item()
            reference = ref(PhysicalObjectKind.LINE, line.line_id)
            size = (
                128 + len(line.text) * 4
                if len(line.text) <= self.options.max_value_chars
                else -1
            )
            if self.retain(size, indexed=reference in index):
                ending = next(
                    (
                        entry.value
                        for entry in line.metadata
                        if entry.key == "line_ending"
                    ),
                    None,
                )
                self.lines.append(
                    Line(
                        reference,
                        line.line_number,
                        line.line_number,
                        line.text,
                        ending={"none": "", "lf": "\n", "cr": "\r", "crlf": "\r\n"}.get(
                            ending
                        )
                        if isinstance(ending, str)
                        else None,
                    )
                )

        for block in batch.blocks:
            self.seen_item()
            reference = ref(PhysicalObjectKind.BLOCK, block.block_id)
            text = block.text or ""
            size = (
                256 + len(text) * 4 if len(text) <= self.options.max_value_chars else -1
            )
            merged: list[str] = []
            for block_value in block.values:
                if block_value.technical_type_hint == "merged_range":
                    if len(merged) >= 64:
                        self.reasons.add("merged_range_limit")
                        break
                    extracted = scalar(
                        block_value.raw_value, self.options.max_value_chars
                    )
                    if extracted is not None:
                        merged.append(extracted[0])
                        if size >= 0:
                            size += 64 + len(extracted[0]) * 4
            if self.retain(size, indexed=reference in index):
                location = block.location
                sheet = (
                    location.sheet_name
                    if isinstance(location, SheetCellLocation)
                    else None
                )
                self.blocks.append(
                    Block(
                        reference,
                        block.order,
                        block.kind,
                        text,
                        sheet,
                        tuple(merged[:64]),
                    )
                )
                if isinstance(location, LineRangeLocation):
                    self.lines.append(
                        Line(
                            reference,
                            location.line_start,
                            location.line_end,
                            text,
                            block=True,
                        )
                    )

    def bind(self, allowed: set[PhysicalSourceRef]) -> None:
        """Legacy sampling допускается только после terminal allowlist check."""
        if (
            any(table.reference not in allowed for table in self.tables.values())
            or any(node.reference not in allowed for node in self.nodes)
            or any(line.reference not in allowed for line in self.lines)
            or any(block.reference not in allowed for block in self.blocks)
        ):
            self.reasons.add("unindexed_source")
        self.tables = {
            key: table
            for key, table in self.tables.items()
            if table.reference in allowed
        }
        for table in self.tables.values():
            table.rows.extend(row for row, _ in table.tail)
            table.tail.clear()
            table.rows.sort(key=lambda row: row.index)
        self.nodes = [node for node in self.nodes if node.reference in allowed]
        self.lines = [line for line in self.lines if line.reference in allowed]
        self.blocks = [block for block in self.blocks if block.reference in allowed]
