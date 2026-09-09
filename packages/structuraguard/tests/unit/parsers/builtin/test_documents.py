"""Fixture-based contracts XLSX/PDF/DOCX без semantic interpretation."""

from __future__ import annotations

import importlib.util
from dataclasses import replace

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import (
    collect,
    contexts_for,
    metadata_map,
    source_for,
)

from structuraguard.contracts import (
    DocumentBlockLocation,
    ExtensionLocation,
    SheetCellLocation,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import (
    DocxParser,
    PdfParser,
    XlsxParser,
    XlsxParserLimits,
)
from structuraguard.ports import Parser


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "name"),
    [
        (XlsxParser(), zip_bytes(package_parts("xlsx")), "a.xlsx"),
        (DocxParser(), zip_bytes(package_parts("docx")), "a.docx"),
        (PdfParser(), pdf_bytes(pages=2), "a.pdf"),
    ],
    ids=["xlsx", "docx", "pdf"],
)
async def test_documents_contract(parser: Parser, content: bytes, name: str) -> None:
    source = source_for(content, display_name=name)
    probe, parse = contexts_for(source, content, batch_size=1)
    await assert_parser_contract(
        ParserContractCase(
            parser=parser, source=source, probe_context=probe, parse_context=parse
        )
    )


@pytest.mark.anyio
async def test_xlsx_raw_formula_blanks_merged_and_sheet_provenance() -> None:
    content = zip_bytes(package_parts("xlsx"))
    source = source_for(content, display_name="a.xlsx")
    batches = await collect(
        XlsxParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    cells = [c for batch in batches for t in batch.tables for c in t.cells]
    assert [c.value.raw_value.value for c in cells] == [
        " Заголовок ",
        " Заголовок ",
        "001.20",
        "2.20",
        None,
        None,
        "",
        "45292",
    ]
    assert all(
        isinstance(c.value.location, SheetCellLocation)
        and c.value.location.sheet_name == " Sheet "
        for c in cells
    )
    values = [v for batch in batches for block in batch.blocks for v in block.values]
    assert [v.raw_value.value for v in values] == [
        "A1:B1",
        "SUM(A2,1)",
        'WEBSERVICE("https://example.invalid/")',
    ]
    assert metadata_map(cells[-1].metadata)["number_format"] == "mm-dd-yy"
    location = batches[-1].tables[0].location
    assert isinstance(location, SheetCellLocation) and location.sheet_name == "hidden"


@pytest.mark.anyio
async def test_docx_order_runs_and_provenance() -> None:
    content = zip_bytes(package_parts("docx"))
    source = source_for(content, display_name="a.docx")
    batches = await collect(
        DocxParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    blocks = [b for batch in batches for b in batch.blocks]
    assert [(b.order, b.kind.value, b.text) for b in blocks] == [
        (0, "heading", "Заголовок"),
        (1, "paragraph", "001.20"),
        (1, "paragraph", ""),
        (2, "list", " item \traw"),
        (3, "paragraph", "cached result"),
    ]
    assert batches[1].tables[0].cells[0].value.raw_value.value == "001.20"
    assert metadata_map(batches[1].tables[0].cells[0].metadata)["grid_span"] == "2"
    assert all(isinstance(b.location, ExtensionLocation) for b in blocks)
    assert blocks[-1].values[0].technical_type_hint == "field_instruction"


@pytest.mark.anyio
async def test_pdf_text_pages_and_bounding_boxes() -> None:
    content = pdf_bytes(pages=2)
    source = source_for(content, display_name="a.pdf")
    batches = await collect(
        PdfParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    lines = [line for batch in batches for b in batch.blocks for line in b.lines]
    assert [line.text for line in lines] == ["Physical text 001.20"] * 2
    for index, line in enumerate(lines):
        assert isinstance(line.location, DocumentBlockLocation)
        assert line.location.bounding_box is not None
        assert line.location.page_number == index + 1


def test_document_factories_are_independent() -> None:
    assert [p.adapter_id for p in (XlsxParser(), PdfParser(), DocxParser())] == [
        "builtin.xlsx",
        "builtin.pdf",
        "builtin.docx",
    ]


@pytest.mark.anyio
async def test_xlsx_shared_strings_and_table_definitions() -> None:
    parts = package_parts("xlsx")
    parts["xl/sharedStrings.xml"] = (
        b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><r><t>01</t></r><r><t> raw</t></r><rPh><t>pronunciation</t></rPh></si></sst>'
    )
    parts["xl/worksheets/sheet1.xml"] = (
        b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>'
    )
    parts["xl/worksheets/_rels/sheet1.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="t" Type="table" Target="../tables/table1.xml"/></Relationships>'
    )
    parts["xl/tables/table1.xml"] = (
        b'<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" name="RawTable" ref="A1:A2" headerRowCount="1"><tableColumns><tableColumn name="RawHeading"/></tableColumns></table>'
    )
    content = zip_bytes(parts)
    source = source_for(content, display_name="a.xlsx")
    batches = await collect(XlsxParser(), source, contexts_for(source, content)[1])
    assert batches[0].tables[0].cells[0].value.raw_value.value == "01 raw"
    assert (
        metadata_map(batches[0].tables[0].cells[0].metadata)["shared_string_index"]
        == "0"
    )
    assert [v.technical_type_hint for v in batches[0].blocks[0].values] == [
        "table_name",
        "table_range",
        "column_name_candidate",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content,name",
    [
        (XlsxParser(), zip_bytes(package_parts("xlsx")), "xlsx"),
        (DocxParser(), zip_bytes(package_parts("docx")), "docx"),
        (PdfParser(), pdf_bytes(), "pdf"),
    ],
    ids=["xlsx", "docx", "pdf"],
)
async def test_document_missing_extra_and_record_limits(
    parser: Parser, content: bytes, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(content, display_name=f"a.{name}")
    probe, context = contexts_for(source, content)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    with pytest.raises(ParserError, match="PARSER_DEPENDENCY_UNAVAILABLE"):
        await parser.probe(source, probe)
    if name != "pdf":
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect(parser, source, replace(context, max_records=1))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser", [XlsxParser(), DocxParser(), PdfParser()], ids=["xlsx", "docx", "pdf"]
)
async def test_spoofed_extension_and_malformed_document(parser: Parser) -> None:
    content = b"secret-canary not a document"
    source = source_for(content, display_name="spoof.xlsx")
    probe, context = contexts_for(source, content)
    assert not (await parser.probe(source, probe)).supported
    with pytest.raises(ParserError, match="PARSER_MALFORMED_INPUT") as failure:
        await collect(parser, source, context)
    assert "secret-canary" not in str(failure.value.details)


@pytest.mark.anyio
async def test_probe_budget_is_not_document_validation() -> None:
    content = zip_bytes(package_parts("xlsx"))
    source = source_for(content, display_name="a.xlsx")
    probe, _ = contexts_for(source, content)
    assert not (
        await XlsxParser(limits=XlsxParserLimits(max_probe_bytes=32)).probe(
            source, probe
        )
    ).supported
    result = await XlsxParser().probe(source, probe)
    assert result.supported
    assert "DOCUMENT_STRUCTURE_VALIDATED_ON_PARSE" in result.warnings
