"""Недостающие aggregate checks приёмки M4: A–E в одной registry."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    builtin_delimited_parsers,
    builtin_document_parsers,
    builtin_json_parsers,
    builtin_markup_parsers,
    builtin_text_parsers,
)


@pytest.mark.anyio
@pytest.mark.parametrize("reverse_order", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    "content,expected",
    [
        (b"ordinary unstructured prose\n", "builtin.text"),
        (b"# Heading\n\nParagraph\n", "builtin.markdown"),
        (
            b"2026-09-02 10:45:01 INFO first\n2026-09-02 10:45:02 ERROR second\n",
            "builtin.log",
        ),
        (b"a,b\n1,2\n3,4\n", "builtin.delimited"),
        (b"a\tb\n1\t2\n3\t4\n", "builtin.delimited"),
        (b'{"a":[1,2]}', "builtin.json"),
        (b'{"a":1}\n{"a":2}\n', "builtin.json-lines"),
        (b"<root><child>raw</child></root>", "builtin.xml"),
        (b"<!DOCTYPE html><html><body><h1>raw</h1></body></html>", "builtin.html"),
        (b"---\nkey: value\nother: [one, two]\n", "builtin.yaml"),
        (zip_bytes(package_parts("xlsx")), "builtin.xlsx"),
        (zip_bytes(package_parts("docx")), "builtin.docx"),
        (pdf_bytes(), "builtin.pdf"),
    ],
    ids=[
        "txt",
        "md",
        "log",
        "csv",
        "tsv",
        "json",
        "ndjson",
        "xml",
        "html",
        "yaml",
        "xlsx",
        "docx",
        "pdf",
    ],
)
async def test_all_core_adapters_select_by_content_independently_of_registration_order(
    content: bytes,
    expected: str,
    reverse_order: bool,
) -> None:
    source = source_for(
        content, display_name="misleading.dat", media_type="application/octet-stream"
    )
    parsers = (
        *builtin_text_parsers(),
        *builtin_delimited_parsers(),
        *builtin_json_parsers(),
        *builtin_markup_parsers(),
        *builtin_document_parsers(),
    )
    registry = ParserRegistry()
    registry.register_many(reversed(parsers) if reverse_order else parsers)
    async with registry.session() as session:
        selected = await session.select(source, contexts_for(source, content)[0])
        assert selected.adapter_id == expected
        assert selected.probe_result.supported


def test_parser_implementations_cannot_reference_semantic_plan_or_normalized_types() -> (
    None
):
    root = Path(__file__).resolve().parents[4] / "src" / "structuraguard" / "parsers"
    forbidden = {
        "ParsePlan",
        "ValidatedParsePlan",
        "NormalizedBatch",
        "NormalizedRecord",
        "MappingPlan",
        "ValidatedMappingPlan",
    }
    paths = tuple(root.rglob("*.py"))
    assert paths
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden, (path.name, node.lineno, node.id)
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, (path.name, node.lineno, node.attr)
            elif isinstance(node, ast.ImportFrom):
                assert not {alias.name for alias in node.names} & forbidden, (
                    path.name,
                    node.lineno,
                )
