"""Физические WordprocessingML blocks/runs/tables в package order."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from xml.etree.ElementTree import Element

from structuraguard.contracts.source import (
    ExtensionLocation,
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedCell,
    ExtractedTable,
    ProbeResult,
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

_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True, slots=True, kw_only=True)
class DocxParserLimits(DocumentParserLimits):
    """Конечные границы Word blocks, runs и cells."""

    max_blocks: int = 100000
    max_runs_per_paragraph: int = 10000
    max_table_cells: int = 100000

    def __post_init__(self) -> None:
        DocumentParserLimits.__post_init__(self)
        for name in ("max_blocks", "max_runs_per_paragraph", "max_table_cells"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 1000000:
                raise ValueError(f"{name} вне диапазона 1..1000000")


class _Word:
    def __init__(self, budget: DocumentBudget, limits: DocxParserLimits) -> None:
        self.budget, self.limits = budget, limits
        self.blocks = self.cells = 0

    def location(self, path: str, order: int) -> ExtensionLocation:
        return ExtensionLocation(
            source=self.budget.request.source.ref,
            namespace="docx:part",
            metadata=metadata(part="word/document.xml", path=path, block_index=order),
        )

    def paragraph(self, paragraph: Element, path: str, order: int) -> ExtractedBlock:
        self.blocks += 1
        self.budget.check("blocks", self.blocks, self.limits.max_blocks)
        loc = self.location(path, order)
        pieces: list[str] = []
        values = []
        for index, run in enumerate(paragraph.iter(_NS + "r")):
            self.budget.check(
                "runs_per_paragraph", index + 1, self.limits.max_runs_per_paragraph
            )
            for child_index, node in enumerate(run):
                local = node.tag.removeprefix(_NS)
                if local in {"t", "delText", "instrText"}:
                    text = node.text or ""
                elif local in {"br", "cr", "tab"}:
                    text = "\t" if local == "tab" else "\n"
                elif local in {"noBreakHyphen", "softHyphen"}:
                    text = "\u2011" if local == "noBreakHyphen" else "\u00ad"
                elif local in {"object", "pict"}:
                    raise unsupported("embedded_word_object")
                elif local in {
                    "rPr",
                    "fldChar",
                    "drawing",
                    "lastRenderedPageBreak",
                    "footnoteReference",
                    "endnoteReference",
                    "commentReference",
                }:
                    # Style/layout и внешние document parts не входят в text scope.
                    continue
                else:
                    raise unsupported("word_run_element")
                # Run index — физический ordinal descendant, не выдуманный XPath.
                run_loc = ExtensionLocation(
                    source=self.budget.request.source.ref,
                    namespace="docx:run",
                    metadata=metadata(
                        part="word/document.xml",
                        paragraph_path=path,
                        run_index=index,
                        child_index=child_index,
                        tag=local,
                        block_index=order,
                    ),
                )
                values.append(
                    self.budget.value(
                        text,
                        run_loc,
                        "field_instruction" if local == "instrText" else "run_text",
                    )
                )
                if local != "instrText":
                    pieces.append(text)
        text = "".join(pieces)
        self.budget.check("paragraph_chars", len(text), self.limits.max_value_chars)
        style = paragraph.find(f"{_NS}pPr/{_NS}pStyle")
        style_id = style.get(_NS + "val", "") if style is not None else ""
        numbering = paragraph.find(f"{_NS}pPr/{_NS}numPr")
        level = numbering.find(_NS + "ilvl") if numbering is not None else None
        num_id = numbering.find(_NS + "numId") if numbering is not None else None
        kind = ExtractedBlockKind.PARAGRAPH
        if style_id.lower().startswith("heading"):
            kind = ExtractedBlockKind.HEADING
        elif numbering is not None:
            kind = ExtractedBlockKind.LIST
        return ExtractedBlock(
            block_id=self.budget.identity("block"),
            kind=kind,
            order=order,
            location=loc,
            text=text,
            values=tuple(values),
            metadata=metadata(
                style_id=style_id,
                list_id=num_id.get(_NS + "val") if num_id is not None else None,
                list_level=level.get(_NS + "val") if level is not None else None,
            ),
        )

    def table(self, node: Element, path: str, order: int) -> DocumentUnit:
        self.blocks += 1
        self.budget.check("blocks", self.blocks, self.limits.max_blocks)
        table_id = self.budget.identity("table")
        cells: list[ExtractedCell] = []
        blocks: list[ExtractedBlock] = []
        nested: list[ExtractedTable] = []
        if any(
            child.tag not in {_NS + "tblPr", _NS + "tblGrid", _NS + "tr"}
            for child in node
        ):
            raise unsupported("word_table_element")
        for row_index, row in enumerate(node.findall(_NS + "tr")):
            if any(child.tag not in {_NS + "trPr", _NS + "tc"} for child in row):
                raise unsupported("word_row_element")
            for column_index, cell in enumerate(row.findall(_NS + "tc")):
                self.cells += 1
                self.budget.check(
                    "table_cells", self.cells, self.limits.max_table_cells
                )
                self.budget.check(
                    "unit_cells",
                    len(cells) + 1,
                    min(self.limits.max_subtree_nodes, self.limits.max_batch_nodes),
                )
                cell_path = f"{path}/w:tr[{row_index + 1}]/w:tc[{column_index + 1}]"
                texts: list[str] = []
                for child_index, child in enumerate(cell):
                    child_path = f"{cell_path}/*[{child_index + 1}]"
                    if child.tag == _NS + "p":
                        paragraph = self.paragraph(child, child_path, order)
                        blocks.append(paragraph)
                        texts.append(paragraph.text or "")
                    elif child.tag == _NS + "tbl":
                        unit = self.table(child, child_path, order)
                        blocks.extend(unit.blocks)
                        nested.extend(unit.tables)
                    elif child.tag != _NS + "tcPr":
                        # Вложенный content control нельзя превращать в пустую cell.
                        raise unsupported("word_cell_element")
                location = self.location(cell_path, order)
                span = cell.find(f"{_NS}tcPr/{_NS}gridSpan")
                merge = cell.find(f"{_NS}tcPr/{_NS}vMerge")
                cells.append(
                    ExtractedCell(
                        cell_id=self.budget.identity("cell"),
                        row_index=row_index,
                        column_index=column_index,
                        value=self.budget.value(
                            "\n".join(texts), location, "cell_text"
                        ),
                        metadata=metadata(
                            grid_span=span.get(_NS + "val", "1")
                            if span is not None
                            else "1",
                            vertical_merge=merge.get(_NS + "val", "continue")
                            if merge is not None
                            else None,
                        ),
                    )
                )
        table = ExtractedTable(
            table_id=table_id,
            location=self.location(path, order),
            cells=tuple(cells),
            metadata=metadata(block_index=order, part="word/document.xml", path=path),
        )
        return DocumentUnit(tables=(table, *nested), blocks=tuple(blocks))


def extract_docx(
    package: SafePackage, budget: DocumentBudget, limits: DocxParserLimits
) -> Iterator[DocumentUnit]:
    try:
        from docx.oxml.ns import qn
    except ImportError:
        raise missing_extra("office") from None
    package.require_format("docx")
    root = package.xml("word/document.xml")
    if root.tag != qn("w:document"):
        raise malformed("invalid_ooxml")
    body = root.find(_NS + "body")
    if body is None:
        raise malformed("invalid_ooxml")
    word = _Word(budget, limits)
    records = 0
    for order, node in enumerate(body):
        if node.tag in {_NS + "p", _NS + "tbl"}:
            records += 1
            budget.check("records", records, budget.request.max_records)
        path = f"/w:document/w:body/*[{order + 1}]"
        if node.tag == _NS + "p":
            yield DocumentUnit(blocks=(word.paragraph(node, path, order),))
        elif node.tag == _NS + "tbl":
            yield word.table(node, path, order)
        elif node.tag != _NS + "sectPr":
            # Не пропускать молча content controls, revisions или altChunk.
            raise unsupported("word_body_element")
        node.clear()
    if "docProps/core.xml" in package.names:
        for index, property_node in enumerate(package.xml("docProps/core.xml")):
            location = ExtensionLocation(
                source=budget.request.source.ref,
                namespace="docx:property",
                metadata=metadata(
                    part="docProps/core.xml",
                    property_index=index,
                    tag=property_node.tag,
                ),
            )
            yield DocumentUnit(
                blocks=(
                    ExtractedBlock(
                        block_id=budget.identity("block"),
                        kind=ExtractedBlockKind.METADATA,
                        order=len(body) + index,
                        location=location,
                        values=(
                            budget.value(
                                property_node.text or "", location, "document_property"
                            ),
                        ),
                    ),
                ),
                records=0,
            )


class DocxParser:
    """Извлечь DOCX в исходном порядке blocks/tables без Word automation.

    Args:
        limits: DocxParserLimits, включая ZIP/worker budgets; ``None`` — defaults.

    Требуется extra ``office``. Paragraphs, headings, lists, runs и nested cells
    сохраняются без назначения бизнес-полей. Макросы и внешние relationships
    не выполняются и не загружаются. POSIX worker не является sandbox.

    Raises:
        ParserError: Отсутствует extra (``PARSER_DEPENDENCY_UNAVAILABLE``),
            некорректен документ (``PARSER_MALFORMED_INPUT``), истёк deadline
            (``PROCESSING_TIMEOUT``) или повреждён worker output
            (``PARSER_OUTPUT_INVALID``).
        SecurityPolicyError: Запрещён ввод (``SECURITY_INPUT_REJECTED``), превышен
            бюджет (``SECURITY_LIMIT_EXCEEDED``) или запрошен пока недоступный
            strict sandbox (``SECURITY_SANDBOX_REQUIRED``).
    """

    adapter_id = "builtin.docx"
    version = "1.0.0"
    priority = 100

    def __init__(self, *, limits: DocxParserLimits | None = None) -> None:
        self.limits = limits or DocxParserLimits()

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по bounded ZIP directory и content types source.

        Context предоставляет snapshot reader и лимит чтения, без загрузки Word.
        """

        return await document_probe(source, context, self.limits, "docx")

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch с block/table/cell provenance для source.

        Context задаёт reader и общие пределы; batch_size — число body blocks.
        Worker, typed errors и cancellation действуют при итерации.
        """

        return document_batches(source, context, self.limits, "docx")


__all__ = ("DocxParser", "DocxParserLimits")
