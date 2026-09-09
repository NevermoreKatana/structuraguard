"""Точные record/container boundaries E через общий registry contract."""

from __future__ import annotations

from typing import Literal

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts import ExtractedBatch
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    DocxParser,
    DocxParserLimits,
    PdfParser,
    PdfParserLimits,
    XlsxParser,
    XlsxParserLimits,
)
from structuraguard.ports import Parser


def document_case(
    format_id: Literal["xlsx", "docx", "pdf"], count: int
) -> tuple[Parser, bytes, str]:
    if format_id == "pdf":
        return (
            PdfParser(limits=PdfParserLimits(max_pages=2)),
            pdf_bytes(pages=count),
            "pages",
        )
    parts = package_parts(format_id)
    if format_id == "xlsx":
        rows = "".join(
            f'<row r="{i}"><c r="A{i}" t="inlineStr"><is><t>00{i}</t></is></c></row>'
            for i in range(1, count + 1)
        )
        parts["xl/worksheets/sheet1.xml"] = (
            f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>'.encode()
        )
        return (
            XlsxParser(limits=XlsxParserLimits(max_cells=2)),
            zip_bytes(parts),
            "cells",
        )
    paragraphs = "".join(
        f"<w:p><w:r><w:t>00{i}</w:t></w:r></w:p>" for i in range(count)
    )
    parts["word/document.xml"] = (
        f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{paragraphs}</w:body></w:document>'.encode()
    )
    return DocxParser(limits=DocxParserLimits(max_blocks=2)), zip_bytes(parts), "blocks"


@pytest.mark.anyio
@pytest.mark.parametrize("format_id", ["xlsx", "docx", "pdf"])
@pytest.mark.parametrize(
    "count", [1, 2, 3], ids=["limit-minus-one", "limit", "limit-plus-one"]
)
async def test_document_exact_limit_preserves_typed_resource_and_no_terminal_on_failure(
    format_id: Literal["xlsx", "docx", "pdf"], count: int
) -> None:
    parser, content, resource = document_case(format_id, count)
    source = source_for(content, display_name=f"a.{format_id}")
    probe, context = contexts_for(source, content, batch_size=1)
    registry = ParserRegistry()
    registry.register(parser)
    batches: list[ExtractedBatch] = []
    async with registry.session() as session:
        selected = await session.select(source, probe)
        stream = selected.parse(source, context)
        if count > 2:
            with pytest.raises(
                SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"
            ) as failure:
                async for batch in stream:
                    batches.append(batch)
            assert failure.value.details["resource"] == resource
            assert failure.value.details["limit"] == 2
            assert not stream.completed
            assert all(
                not batch.is_last and batch.manifest is None for batch in batches
            )
        else:
            async for batch in stream:
                batches.append(batch)
            assert stream.completed
            manifest = batches[-1].manifest
            assert manifest is not None and manifest.record_count == count
            manifest.validate_batches(batches)


@pytest.mark.anyio
@pytest.mark.parametrize("format_id", ["xlsx", "docx"])
async def test_empty_ooxml_container_passes_shared_parser_contract(
    format_id: Literal["xlsx", "docx"],
) -> None:
    parser, content, _ = document_case(format_id, 0)
    source = source_for(content, display_name=f"empty.{format_id}")
    probe, context = contexts_for(source, content)
    batches = await assert_parser_contract(
        ParserContractCase(
            parser=parser, source=source, probe_context=probe, parse_context=context
        )
    )
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 0
    assert not [
        cell for batch in batches for table in batch.tables for cell in table.cells
    ]
    assert not [block for batch in batches for block in batch.blocks]
