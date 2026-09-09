"""Read-only OOXML: raw cells, cached values и formula source без вычисления."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from xml.etree.ElementTree import Element

from structuraguard.contracts.common import NullScalar
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedCell,
    ExtractedTable,
    ExtractedValue,
    ProbeResult,
    SheetCellLocation,
    SourceArtifact,
)
from structuraguard.ports.source import ParseContext, ProbeContext

from ._documents import (
    DocumentBudget,
    DocumentParserLimits,
    DocumentUnit,
    SafePackage,
    document_batches,
    document_probe,
    metadata,
    unsupported,
)
from ._markup import malformed, missing_extra

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


@dataclass(frozen=True, slots=True, kw_only=True)
class XlsxParserLimits(DocumentParserLimits):
    """Пределы физических координат и контейнера, независимо от dimension hints."""

    max_sheets: int = 128
    max_rows_per_sheet: int = 1_048_576
    max_columns: int = 16384
    max_cells: int = 1_000_000
    max_shared_strings: int = 100000
    max_merged_ranges: int = 10000

    def __post_init__(self) -> None:
        DocumentParserLimits.__post_init__(self)
        for name, cap in (
            ("max_sheets", 1024),
            ("max_rows_per_sheet", 1048576),
            ("max_columns", 16384),
            ("max_cells", 10000000),
            ("max_shared_strings", 1000000),
            ("max_merged_ranges", 100000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"{name} вне диапазона 1..{cap}")


def _coordinate(
    address: str, budget: DocumentBudget, limits: XlsxParserLimits
) -> tuple[int, int]:
    from openpyxl.utils.cell import coordinate_to_tuple

    if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", address):
        raise malformed("invalid_ooxml")
    row, column = coordinate_to_tuple(address)
    budget.check("rows_per_sheet", row, limits.max_rows_per_sheet)
    budget.check("columns", column, limits.max_columns)
    return row - 1, column - 1


def _rich_text(element: Element) -> str:
    # rPh — pronunciation annotation, не отображаемое cell value.
    pieces: list[str] = []
    for child in element:
        if child.tag == _NS + "t":
            pieces.append(child.text or "")
        elif child.tag == _NS + "r":
            pieces.extend(node.text or "" for node in child.findall(_NS + "t"))
    return "".join(pieces)


def extract_xlsx(
    package: SafePackage, budget: DocumentBudget, limits: XlsxParserLimits
) -> Iterator[DocumentUnit]:
    """Материализуется только ограниченный member; rows выходят сегментами."""

    try:
        from openpyxl.styles.numbers import BUILTIN_FORMATS
    except ImportError:
        raise missing_extra("excel") from None
    package.require_format("xlsx")
    workbook = package.xml("xl/workbook.xml")
    if workbook.tag != _NS + "workbook":
        raise malformed("invalid_ooxml")
    sheets = workbook.findall(f"{_NS}sheets/{_NS}sheet")
    budget.check("sheets", len(sheets), limits.max_sheets)
    relationships = package.relationships("xl/workbook.xml")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in package.names:
        for item in package.xml("xl/sharedStrings.xml"):
            budget.check("shared_strings", len(shared) + 1, limits.max_shared_strings)
            shared.append(budget.text(_rich_text(item)))
    styles: list[str] = []
    if "xl/styles.xml" in package.names:
        root = package.xml("xl/styles.xml")
        formats = {str(key): value for key, value in BUILTIN_FORMATS.items()}
        formats.update(
            {
                e.get("numFmtId", ""): e.get("formatCode", "")
                for e in root.findall(f"{_NS}numFmts/{_NS}numFmt")
            }
        )
        styles = [
            formats.get(e.get("numFmtId", "0"), "")
            for e in root.findall(f"{_NS}cellXfs/{_NS}xf")
        ]
    seen_sheets: set[str] = set()
    cells_seen = records = 0

    def location(row: int = 0, column: int = 0) -> SheetCellLocation:
        return SheetCellLocation(
            source=budget.request.source.ref,
            sheet_name=name,
            row_index=row,
            column_index=column,
        )

    def unit(*, last: bool) -> DocumentUnit:
        properties = workbook.find(_NS + "workbookPr")
        table = ExtractedTable(
            table_id=table_id,
            location=location(),
            cells=tuple(cells),
            metadata=metadata(
                sheet_index=_sheet_index,
                sheet_name_raw=name,
                visibility=sheet.get("state", "visible"),
                part=part,
                date1904=properties.get("date1904", "0")
                if properties is not None
                else "0",
            ),
            segment_index=segment_index if rows else None,
            row_start_index=segment_start if rows else None,
            row_end_index=previous_row if rows else None,
            is_last_segment=last if rows else None,
        )
        return DocumentUnit(tables=(table,), blocks=tuple(blocks), records=count)

    for _sheet_index, sheet in enumerate(sheets):
        name = sheet.get("name", "")
        if not name or name in seen_sheets or len(name) > 255:
            raise malformed("invalid_ooxml")
        seen_sheets.add(name)
        part = relationships.get(sheet.get(_RID, ""), "")
        root = package.xml(part)
        if root.tag != _NS + "worksheet":
            raise unsupported("sheet_type")
        table_id = budget.identity("table")

        descriptions: list[ExtractedBlock] = []
        for index, merged in enumerate(root.findall(f"{_NS}mergeCells/{_NS}mergeCell")):
            budget.check("merged_ranges", index + 1, limits.max_merged_ranges)
            ref = merged.get("ref", "")
            bounds = ref.split(":")
            start = _coordinate(bounds[0], budget, limits)
            end = _coordinate(bounds[-1], budget, limits)
            if len(bounds) > 2 or start[0] > end[0] or start[1] > end[1]:
                raise malformed("invalid_ooxml")
            loc = location(*start)
            descriptions.append(
                ExtractedBlock(
                    block_id=budget.identity("block"),
                    kind=ExtractedBlockKind.METADATA,
                    order=index,
                    location=loc,
                    values=(budget.value(ref, loc, "merged_range"),),
                    metadata=metadata(sheet_table_id=table_id),
                )
            )
        for target in package.relationships(part).values():
            if not target.startswith("xl/tables/"):
                continue
            definition = package.xml(target)
            ref = definition.get("ref", "")
            start = _coordinate(ref.split(":")[0], budget, limits)
            _coordinate(ref.split(":")[-1], budget, limits)
            loc = location(*start)
            values = [
                budget.value(definition.get("name", ""), loc, "table_name"),
                budget.value(ref, loc, "table_range"),
            ]
            values.extend(
                budget.value(c.get("name", ""), loc, "column_name_candidate")
                for c in definition.findall(f"{_NS}tableColumns/{_NS}tableColumn")
            )
            descriptions.append(
                ExtractedBlock(
                    block_id=budget.identity("block"),
                    kind=ExtractedBlockKind.METADATA,
                    order=len(descriptions),
                    location=loc,
                    values=tuple(values),
                    metadata=metadata(
                        sheet_table_id=table_id,
                        part=target,
                        header_row_count_candidate=definition.get(
                            "headerRowCount", "1"
                        ),
                    ),
                )
            )
        rows = root.findall(f"{_NS}sheetData/{_NS}row")
        previous_row = -1
        segment_start = segment_index = count = 0
        cells: list[ExtractedCell] = []
        blocks = descriptions

        for row in rows:
            raw_row = row.get("r", "")
            if not raw_row.isascii() or not raw_row.isdigit() or len(raw_row) > 7:
                raise malformed("invalid_ooxml")
            row_index = int(raw_row) - 1
            if row_index <= previous_row:
                raise malformed("invalid_ooxml")
            budget.check("rows_per_sheet", row_index + 1, limits.max_rows_per_sheet)
            if count >= budget.request.batch_size:
                yield unit(last=False)
                segment_index += 1
                segment_start = previous_row + 1
                cells, blocks, count = [], [], 0
            previous_row = row_index
            records += 1
            budget.check("records", records, budget.request.max_records)
            count += 1
            previous_column = -1
            for cell in row.findall(_NS + "c"):
                budget.check(
                    "unit_cells",
                    len(cells) + len(blocks) + 2,
                    min(limits.max_subtree_nodes, limits.max_batch_nodes),
                )
                cells_seen += 1
                budget.check("cells", cells_seen, limits.max_cells)
                address = cell.get("r", "")
                cell_row, column = _coordinate(address, budget, limits)
                if cell_row != row_index or column <= previous_column:
                    raise malformed("invalid_ooxml")
                previous_column = column
                loc = location(row_index, column)
                cell_type = cell.get("t", "n")
                stored = cell.find(_NS + "v")
                inline = cell.find(_NS + "is")
                formula = cell.find(_NS + "f")
                raw = stored.text or "" if stored is not None else None
                text = raw
                if cell_type == "s" and raw is not None:
                    if (
                        not raw.isascii()
                        or not raw.isdigit()
                        or len(raw) > 9
                        or int(raw) >= len(shared)
                    ):
                        raise malformed("invalid_ooxml")
                    text = shared[int(raw)]
                elif cell_type == "inlineStr" and inline is not None:
                    text = _rich_text(inline)
                style = cell.get("s", "0")
                if (
                    not style.isascii()
                    or not style.isdigit()
                    or len(style) > 9
                    or (styles and int(style) >= len(styles))
                ):
                    raise malformed("invalid_ooxml")
                value = (
                    budget.value(
                        text,
                        loc,
                        "cached_formula_value"
                        if formula is not None
                        else "stored_cell_value",
                    )
                    if text is not None
                    else ExtractedValue(
                        value_id=budget.identity("value"),
                        raw_value=NullScalar(),
                        location=loc,
                    )
                )
                cell_id = budget.identity("cell")
                cells.append(
                    ExtractedCell(
                        cell_id=cell_id,
                        row_index=row_index,
                        column_index=column,
                        value=value,
                        metadata=metadata(
                            address=address,
                            cell_type=cell_type,
                            stored_value_present=stored is not None,
                            formula_present=formula is not None,
                            shared_string_index=raw if cell_type == "s" else None,
                            style_id=style,
                            number_format=styles[int(style)] if styles else None,
                        ),
                    )
                )
                if formula is not None:
                    blocks.append(
                        ExtractedBlock(
                            block_id=budget.identity("block"),
                            kind=ExtractedBlockKind.METADATA,
                            order=row_index,
                            location=loc,
                            values=(
                                budget.value(formula.text or "", loc, "formula_source"),
                            ),
                            metadata=metadata(
                                cell_id=cell_id,
                                formula_type=formula.get("t", "normal"),
                                shared_index=formula.get("si"),
                                formula_ref=formula.get("ref"),
                            ),
                        )
                    )
            row.clear()
        yield unit(last=True)


class XlsxParser:
    """Извлечь физические листы, таблицы и ячейки XLSX без вычисления формул.

    Args:
        limits: XlsxParserLimits, включая ZIP/worker budgets; ``None`` — defaults.

    Требуется extra ``excel``. Bounded read-only OOXML parts читаются в POSIX
    worker, который не является sandbox. Merged ranges не размножаются в cells;
    formula source и сохранённый cached value раздельны, пересчёта нет.

    Raises:
        ParserError: Отсутствует extra (``PARSER_DEPENDENCY_UNAVAILABLE``),
            некорректен документ (``PARSER_MALFORMED_INPUT``), истёк deadline
            (``PROCESSING_TIMEOUT``) или повреждён worker output
            (``PARSER_OUTPUT_INVALID``).
        SecurityPolicyError: Запрещён ввод (``SECURITY_INPUT_REJECTED``), превышен
            бюджет (``SECURITY_LIMIT_EXCEEDED``) или запрошен пока недоступный
            strict sandbox (``SECURITY_SANDBOX_REQUIRED``).
    """

    adapter_id = "builtin.xlsx"
    version = "1.0.0"
    priority = 100

    def __init__(self, *, limits: XlsxParserLimits | None = None) -> None:
        self.limits = limits or XlsxParserLimits()

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по bounded ZIP directory и content types source.

        Reader и лимит чтения задаёт context; проверка не вычисляет cells.
        """

        return await document_probe(source, context, self.limits, "xlsx")

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch с sheet/cell provenance для source.

        Context задаёт reader и общие пределы; batch_size измеряется в rows.
        Worker запускается при итерации; ошибки и cancellation освобождают
        временный snapshot worker, не удаляя исходный файл caller.
        """

        return document_batches(source, context, self.limits, "xlsx")


__all__ = ("XlsxParser", "XlsxParserLimits")
