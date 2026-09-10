"""Детерминированные гипотезы таблиц по физическим rows, без normalization."""

from collections import Counter
from decimal import Decimal
from typing import Literal

from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.structure import FieldObservation, TabularObservation
from structuraguard.structure._observations import (
    Observations,
    counts,
    field_name,
    ratio,
)
from structuraguard.structure._samples import Row, Table


def _summary(row: Row) -> bool:
    return bool(row.cells) and row.cells[0][1].strip().casefold() in {
        "total",
        "subtotal",
        "grand total",
        "summary",
        "итого",
        "всего",
    }


def _signature(row: Row) -> tuple[tuple[int, str], ...]:
    return tuple((index, value) for index, value, _ in row.cells)


def analyze_tables(output: Observations) -> None:
    for table_id, table in output.samples.tables.items():
        _table(output, table_id, table)


def _table(output: Observations, table_id: str, table: Table) -> None:
    if not table.seen:
        return
    width = min(
        [item for item in table.widths if item] or [0],
        key=lambda item: (-table.widths[item], -item),
    )
    refs = (table.reference,)
    output.add(
        TabularObservation(
            source_refs=refs,
            role="shape",
            table_id=table_id,
            row_start=table.start,
            row_end=table.end,
            row_count=table.seen,
            column_count=width,
            ragged_rows=sum(
                count for columns, count in table.widths.items() if columns != width
            )
            + table.sparse_widths[width],
            widths=counts(
                Counter(
                    {str(columns): count for columns, count in table.widths.items()}
                )
            ),
        ),
        Decimal("1"),
    )
    if not table.rows:
        return
    headers: list[Row] = []
    numeric_kinds = {"integer", "decimal", "boolean", "date", "timestamp"}
    for row in table.rows[:8]:
        if not row.cells or len(row.cells) != width or _summary(row):
            continue
        string_count = sum(
            bool(value.strip()) and hint == "string" for _, value, hint in row.cells
        )
        if string_count < max(1, (width * 3 + 3) // 4):
            continue
        headers.append(row)
        following = [other for other in table.rows if other.index > row.index][:8]
        contrast = any(
            any(hint in numeric_kinds for _, _, hint in other.cells)
            for other in following
        )
        confidence = Decimal("0.85") if contrast else Decimal("0.55")
        output.add(
            TabularObservation(
                source_refs=refs,
                role="header",
                table_id=table_id,
                row_start=row.index,
                row_end=row.index,
                row_count=1,
                column_count=width,
            ),
            confidence,
            candidate=ParsePlanKind.TABULAR,
        )
    first_header = headers[0] if headers else None
    repeated: set[int] = set()
    if first_header:
        signature = _signature(first_header)
        for row in table.rows:
            if row.index != first_header.index and _signature(row) == signature:
                repeated.add(row.index)
                output.add(
                    TabularObservation(
                        source_refs=refs,
                        role="repeated_header",
                        table_id=table_id,
                        row_start=row.index,
                        row_end=row.index,
                        row_count=1,
                        column_count=width,
                    ),
                    Decimal("0.9"),
                )
    data: list[Row] = []
    for row in table.rows:
        role: Literal["empty", "footer", "summary", "meta"] | None = None
        if not row.cells or all(
            not value.strip() and hint == "string" for _, value, hint in row.cells
        ):
            role = "empty"
        elif _summary(row):
            role = "footer" if row.index == table.end else "summary"
        elif first_header is not None and row.index < first_header.index:
            role = "meta"
        if role is not None:
            output.add(
                TabularObservation(
                    source_refs=refs,
                    role=role,
                    table_id=table_id,
                    row_start=row.index,
                    row_end=row.index,
                    row_count=1,
                    column_count=len(row.cells),
                ),
                Decimal("1") if role == "empty" else Decimal("0.65"),
            )
        elif row.index not in repeated and (
            first_header is None or row.index != first_header.index
        ):
            data.append(row)
    if data:
        start = end = data[0].index
        for row in data[1:]:
            if row.index != end + 1:
                output.add(
                    TabularObservation(
                        source_refs=refs,
                        role="data",
                        table_id=table_id,
                        row_start=start,
                        row_end=end,
                        row_count=end - start + 1,
                        column_count=width,
                    ),
                    Decimal("0.7"),
                    candidate=ParsePlanKind.TABULAR,
                )
                start = row.index
            end = row.index
        output.add(
            TabularObservation(
                source_refs=refs,
                role="data",
                table_id=table_id,
                row_start=start,
                row_end=end,
                row_count=end - start + 1,
                column_count=width,
            ),
            Decimal("0.7"),
            candidate=ParsePlanKind.TABULAR,
        )
    labels = (
        {index: value for index, value, _ in first_header.cells} if first_header else {}
    )
    used_names: set[str] = set()
    for column in range(width):
        distribution: Counter[str] = Counter(
            hint for row in data for index, _, hint in row.cells if index == column
        )
        if not distribution:
            continue
        raw = labels.get(column)
        name = field_name(raw or "", f"column_{column}")
        if name in used_names:
            name = f"{name}_{column}"
        used_names.add(name)
        output.add(
            FieldObservation(
                source_refs=refs,
                scope=table_id,
                column_index=column,
                suggested_name=name,
                raw_label=raw,
                sampled_count=distribution.total(),
                types=counts(distribution),
            ),
            ratio(max(distribution.values()), distribution.total()),
        )
    for block in output.samples.blocks:
        if block.merged and block.sheet is not None and block.sheet == table.sheet:
            output.add(
                TabularObservation(
                    source_refs=(table.reference, block.reference),
                    role="merged",
                    table_id=table_id,
                    row_start=table.start,
                    row_end=table.end,
                    row_count=table.seen,
                    column_count=width,
                    merged_ranges=block.merged,
                ),
                Decimal("1"),
            )
