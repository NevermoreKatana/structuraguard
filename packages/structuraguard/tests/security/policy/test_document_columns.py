"""Ширина document tables и YAML mappings подчиняется общей policy."""

from typing import Literal
from uuid import UUID

import pytest
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.exceptions import SecurityPolicyError
from structuraguard.parsers.builtin import DocxParser, PdfParser, YamlParser
from structuraguard.ports.parser import Parser
from structuraguard.security import SecurityLimits, SecurityPolicy, SecuritySession


@pytest.mark.anyio
@pytest.mark.parametrize("format_id", ["yaml", "pdf", "docx"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_columns_exact_and_one_over(
    format_id: Literal["yaml", "pdf", "docx"], extra: int
) -> None:
    parser: Parser
    if format_id == "yaml":
        content, parser = b"a: 1\nb: 2\n", YamlParser()
    elif format_id == "pdf":
        content, parser = pdf_bytes(grid=True), PdfParser()
    else:
        parts = package_parts("docx")
        parts["word/document.xml"] = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            b"<w:body><w:tbl><w:tr>"
            b"<w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>"
            b"<w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc>"
            b"</w:tr></w:tbl></w:body></w:document>"
        )
        content, parser = zip_bytes(parts), DocxParser()
    run = SecuritySession(
        SecurityPolicy(
            limits=SecurityLimits(max_columns=2 - extra),
            allowed_formats=(format_id,),
            parser_trust="trusted",
        ),
        run_id=UUID(int=1),
    )
    source = source_for(content, display_name="source." + format_id)
    _, context = contexts_for(source, content)

    async def parse() -> None:
        async with run.parse(parser, source, context) as batches:
            assert [batch async for batch in batches]

    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await parse()
        assert run.events
    else:
        await parse()
