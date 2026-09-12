"""Один provenance contract для существующих parser/selection adapters."""

from datetime import UTC, datetime

import pytest
from tests.fakes.provenance import provenance_fixture

from structuraguard.contracts.common import ValidationDecision
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    HtmlParser,
    PlainTextParser,
    XmlParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.ports.provenance import ProvenanceValidator as ProvenancePort
from structuraguard.validation import ProvenanceValidator


async def assert_provenance(parser: Parser, content: bytes, expected_kind: str) -> None:
    fixture = await provenance_fixture(content, parser=parser)
    validator: ProvenancePort = ProvenanceValidator()
    report = await validator.validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert report.decision is ValidationDecision.ACCEPTED
    assert report.complete
    assert report.verified_values == report.required_values
    assert any(
        location.location.kind == expected_kind
        for evidence in report.evidence
        for location in evidence.locations
    )
    assert all(evidence.verified for evidence in report.evidence)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content,kind",
    [
        (DelimitedTextParser(), b"name,count\nAda,1\nBob,2\n", "tabular_cell"),
        (
            PlainTextParser(),
            b"metric cpu 1\n  detail one\nmetric cpu 2\n  detail two\n",
            "line_range",
        ),
        (
            XmlParser(),
            b"<root><item><name>Ada</name></item><item><name>Bob</name></item></root>",
            "xpath",
        ),
        (HtmlParser(), b"<p>Name: Ada</p><p>Name: Bob</p>", "css_selector"),
    ],
)
async def test_text_markup_and_table_origins(
    parser: Parser, content: bytes, kind: str
) -> None:
    await assert_provenance(parser, content, kind)


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize("format_id", ["pdf", "docx", "xlsx"])
async def test_document_origins(format_id: str) -> None:
    from tests.unit.parsers.builtin._document_fixtures import (
        package_parts,
        pdf_bytes,
        zip_bytes,
    )

    from structuraguard.parsers.builtin import DocxParser, PdfParser, XlsxParser

    if format_id == "pdf":
        await assert_provenance(PdfParser(), pdf_bytes(pages=2), "document_block")
    elif format_id == "docx":
        parts = package_parts("docx")
        parts["word/document.xml"] = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Name: Ada</w:t></w:r></w:p><w:p><w:r><w:t>Name: Bob</w:t></w:r></w:p></w:body></w:document>'
        )
        await assert_provenance(DocxParser(), zip_bytes(parts), "extension")
    else:
        parts = package_parts("xlsx")
        parts["xl/workbook.xml"] = parts["xl/workbook.xml"].replace(
            b'<sheet name="hidden" sheetId="2" state="hidden" r:id="r2"/>', b""
        )
        parts["xl/worksheets/sheet1.xml"] = (
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>name</t></is></c><c r="B1" t="inlineStr"><is><t>count</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>Ada</t></is></c><c r="B2"><v>1</v></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>Bob</t></is></c><c r="B3"><v>2</v></c></row></sheetData></worksheet>'
        )
        await assert_provenance(XlsxParser(), zip_bytes(parts), "sheet_cell")
