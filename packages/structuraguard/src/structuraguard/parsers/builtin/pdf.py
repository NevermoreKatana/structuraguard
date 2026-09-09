"""Только text-layer PDF: native backend запускается в отдельном worker."""

from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict

from structuraguard.contracts.source import (
    BoundingBox,
    DocumentBlockLocation,
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedCell,
    ExtractedLine,
    ExtractedTable,
    ProbeResult,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError
from structuraguard.ports.source import ParseContext, ProbeContext

from ._documents import (
    DocumentBudget,
    DocumentParserLimits,
    DocumentUnit,
    document_batches,
    document_probe,
    metadata,
    unsupported,
)
from ._markup import malformed, missing_extra, rejected


@dataclass(frozen=True, slots=True, kw_only=True)
class PdfParserLimits(DocumentParserLimits):
    """Caps применяются до page extraction; OCR и rendering отсутствуют."""

    max_pages: int = 1000
    max_objects: int = 100000
    max_blocks_per_page: int = 10000
    max_tables_per_page: int = 100
    max_cells_per_page: int = 10000

    def __post_init__(self) -> None:
        DocumentParserLimits.__post_init__(self)
        for name, cap in (
            ("max_pages", 100000),
            ("max_objects", 1000000),
            ("max_blocks_per_page", 100000),
            ("max_tables_per_page", 1000),
            ("max_cells_per_page", 100000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"{name} вне диапазона 1..{cap}")


class _Rect(Protocol):
    width: float
    height: float


class _TableRow(Protocol):
    cells: list[tuple[float, float, float, float] | None]


class _Table(Protocol):
    bbox: tuple[float, float, float, float]
    cells: list[tuple[float, float, float, float] | None]
    row_count: int
    col_count: int
    rows: list[_TableRow]

    def extract(self) -> list[list[str | None]]: ...


class _Tables(Protocol):
    tables: list[_Table]


class _Page(Protocol):
    rect: _Rect

    def get_text(self, option: str, *, flags: int, sort: bool) -> object: ...
    def find_tables(self) -> _Tables: ...


class _PdfDocument(Protocol):
    needs_pass: bool
    page_count: int
    is_repaired: bool

    def close(self) -> None: ...
    def xref_length(self) -> int: ...
    def embfile_count(self) -> int: ...
    def xref_object(self, number: int, *, compressed: bool) -> str: ...
    def xref_is_stream(self, number: int) -> bool: ...
    def xref_stream_raw(self, number: int) -> bytes: ...
    def xref_stream(self, number: int) -> bytes: ...
    def load_page(self, number: int) -> _Page: ...


class _PdfModule(Protocol):
    TEXTFLAGS_DICT: int
    TEXT_PRESERVE_IMAGES: int

    def open(self, filename: str, *, filetype: str) -> _PdfDocument: ...


class _Span(BaseModel):
    text: str


class _Line(BaseModel):
    bbox: tuple[float, float, float, float]
    spans: tuple[_Span, ...]


class _Block(BaseModel):
    type: int
    bbox: tuple[float, float, float, float]
    lines: tuple[_Line, ...] = ()


class _PageText(BaseModel):
    model_config = ConfigDict(extra="ignore")
    blocks: tuple[_Block, ...]


def _box(
    rect: tuple[float, float, float, float], width: float, height: float
) -> BoundingBox | None:
    if (
        not all(math.isfinite(v) for v in (*rect, width, height))
        or width <= 0
        or height <= 0
    ):
        raise malformed("invalid_pdf")
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return None
    # PDF может содержать текст вне crop box; raw bbox сохраняется в metadata.
    return BoundingBox(
        x=max(0.0, x0 / width),
        y=max(0.0, y0 / height),
        width=(x1 - x0) / width,
        height=(y1 - y0) / height,
    )


def _preflight(
    document: _PdfDocument, budget: DocumentBudget, limits: PdfParserLimits
) -> None:
    if document.needs_pass:
        raise unsupported("encrypted_document")
    if document.is_repaired:
        raise malformed("invalid_pdf")
    budget.check(
        "pages", document.page_count, min(limits.max_pages, budget.request.max_records)
    )
    budget.check("pdf_objects", document.xref_length(), limits.max_objects)
    if document.embfile_count():
        raise rejected("pdf_embedded_file")
    total = 0
    forbidden = {
        "OpenAction",
        "AA",
        "JavaScript",
        "JS",
        "Launch",
        "EmbeddedFiles",
        "EF",
        "RichMedia",
        "XFA",
        "SubmitForm",
        "ImportData",
        "GoToR",
    }
    for index in range(1, document.xref_length()):
        obj = document.xref_object(index, compressed=True)
        budget.check("pdf_object_chars", len(obj), limits.max_value_chars)
        names = re.findall(r"/([^\s<>\[\](){}/%]+)", obj)
        decoded = {
            re.sub(r"#([0-9a-fA-F]{2})", lambda m: chr(int(m[1], 16)), name)
            for name in names
        }
        if forbidden & decoded:
            raise rejected("pdf_action")
        if document.xref_is_stream(index):
            budget.check(
                "pdf_stream_bytes",
                len(document.xref_stream_raw(index)),
                limits.max_member_bytes,
            )
            # Native decompression ограничена также process memory и hard timeout.
            size = len(document.xref_stream(index))
            budget.check("pdf_decoded_stream_bytes", size, limits.max_member_bytes)
            total += size
            budget.check("pdf_decoded_bytes", total, limits.max_extracted_bytes)


def extract_pdf(
    path: str, budget: DocumentBudget, limits: PdfParserLimits
) -> Iterator[DocumentUnit]:
    try:
        import pymupdf

        backend = cast(_PdfModule, pymupdf)
    except ImportError:
        raise missing_extra("pdf") from None
    try:
        document = backend.open(path, filetype="pdf")
    except pymupdf.FileDataError:
        # FileDataError — документированный malformed input, не произвольный RuntimeError.
        raise malformed("invalid_document") from None
    try:
        _preflight(document, budget, limits)
        has_text = False
        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            width, height = page.rect.width, page.rect.height
            parsed = _PageText.model_validate(
                page.get_text(
                    "dict",
                    flags=backend.TEXTFLAGS_DICT & ~backend.TEXT_PRESERVE_IMAGES,
                    sort=False,
                )
            )
            budget.check("page_blocks", len(parsed.blocks), limits.max_blocks_per_page)
            blocks: list[ExtractedBlock] = []
            tables: list[ExtractedTable] = []
            page_id = budget.identity("block")
            page_location = DocumentBlockLocation(
                source=budget.request.source.ref,
                block_id=page_id,
                page_number=page_index + 1,
            )
            blocks.append(
                ExtractedBlock(
                    block_id=page_id,
                    kind=ExtractedBlockKind.METADATA,
                    order=0,
                    location=page_location,
                    text="",
                    metadata=metadata(
                        page_width=width,
                        page_height=height,
                        text_order="content_stream",
                        bbox_units="normalized_crop_box",
                        tables="geometric_candidates",
                    ),
                )
            )
            for block_index, block in enumerate(parsed.blocks):
                if block.type != 0:
                    continue
                block_id = budget.identity("block")
                loc = DocumentBlockLocation(
                    source=budget.request.source.ref,
                    block_id=block_id,
                    page_number=page_index + 1,
                    bounding_box=_box(block.bbox, width, height),
                )
                lines: list[ExtractedLine] = []
                for line_index, line in enumerate(block.lines):
                    text = budget.text("".join(span.text for span in line.spans))
                    has_text |= bool(text.strip())
                    lines.append(
                        ExtractedLine(
                            line_id=budget.identity("line"),
                            line_number=line_index + 1,
                            text=text,
                            location=loc.model_copy(
                                update={"bounding_box": _box(line.bbox, width, height)}
                            ),
                            metadata=metadata(
                                x0=line.bbox[0],
                                y0=line.bbox[1],
                                x1=line.bbox[2],
                                y1=line.bbox[3],
                            ),
                        )
                    )
                blocks.append(
                    ExtractedBlock(
                        block_id=block_id,
                        kind=ExtractedBlockKind.PARAGRAPH,
                        order=block_index + 1,
                        location=loc,
                        text=None if lines else "",
                        lines=tuple(lines),
                        metadata=metadata(
                            x0=block.bbox[0],
                            y0=block.bbox[1],
                            x1=block.bbox[2],
                            y1=block.bbox[3],
                        ),
                    )
                )
            detected = page.find_tables().tables
            budget.check("page_tables", len(detected), limits.max_tables_per_page)
            cell_count = 0
            for table_index, table in enumerate(detected):
                budget.check(
                    "page_cells",
                    cell_count + table.row_count * table.col_count,
                    limits.max_cells_per_page,
                )
                table_id = budget.identity("table")
                cells: list[ExtractedCell] = []
                for row_index, row in enumerate(table.extract()):
                    for column_index, cell_text in enumerate(row):
                        cell_count += 1
                        budget.check(
                            "page_cells", cell_count, limits.max_cells_per_page
                        )
                        if cell_text is None:
                            continue
                        cell_box = table.rows[row_index].cells[column_index]
                        cell_location = page_location.model_copy(
                            update={
                                "bounding_box": _box(cell_box, width, height)
                                if cell_box is not None
                                else None
                            }
                        )
                        cells.append(
                            ExtractedCell(
                                cell_id=budget.identity("cell"),
                                row_index=row_index,
                                column_index=column_index,
                                value=budget.value(
                                    cell_text, cell_location, "table_text_candidate"
                                ),
                                metadata=metadata(
                                    table_index=table_index,
                                    row_index=row_index,
                                    column_index=column_index,
                                ),
                            )
                        )
                tables.append(
                    ExtractedTable(
                        table_id=table_id,
                        location=page_location.model_copy(
                            update={"bounding_box": _box(table.bbox, width, height)}
                        ),
                        cells=tuple(cells),
                        metadata=metadata(
                            table_index=table_index, extraction="geometric_candidate"
                        ),
                    )
                )
            yield DocumentUnit(blocks=tuple(blocks), tables=tuple(tables))
        if not has_text:
            raise ParserError(
                error_code="PARSER_NO_TEXT_LAYER",
                message="PDF не содержит извлекаемого текстового слоя.",
            )
    finally:
        document.close()


class PdfParser:
    """Извлечь text-layer PDF без OCR, JavaScript и рендеринга страниц.

    Args:
        limits: PdfParserLimits, включая page/text/worker budgets; ``None``
            выбирает defaults. Дополнительно действует ParseContext.

    Требуется extra ``pdf`` с PyMuPDF (AGPL/commercial). POSIX worker ограничивает
    ресурсы, но не заменяет sandbox. Table candidates — результат геометрического
    backend detection, не гарантированная таблица или бизнес-схема.

    Raises:
        ParserError: Нет extra (``PARSER_DEPENDENCY_UNAVAILABLE``) или текста
            (``PARSER_NO_TEXT_LAYER``); malformed PDF (``PARSER_MALFORMED_INPUT``),
            deadline (``PROCESSING_TIMEOUT``), crash/output defect worker
            (``PARSER_OUTPUT_INVALID``).
        SecurityPolicyError: Запрещён ввод (``SECURITY_INPUT_REJECTED``), превышен
            бюджет (``SECURITY_LIMIT_EXCEEDED``) или запрошен пока недоступный
            strict sandbox (``SECURITY_SANDBOX_REQUIRED``).
    """

    adapter_id = "builtin.pdf"
    version = "1.0.0"
    priority = 100

    def __init__(self, *, limits: PdfParserLimits | None = None) -> None:
        self.limits = limits or PdfParserLimits()

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по bounded PDF signature из reader/context source.

        Наличие text layer проверяется только при parse, не этим probe.
        """

        return await document_probe(source, context, self.limits, "pdf")

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch с page/block/bbox provenance для source.

        Reader и общие лимиты задаёт context; batch_size измеряется в pages.
        Worker, typed errors и cancellation действуют при итерации.
        """

        return document_batches(source, context, self.limits, "pdf")


__all__ = ("PdfParser", "PdfParserLimits")
