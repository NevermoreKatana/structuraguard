"""Регрессии четырёх findings финального review M4, без semantic processing."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.unit.parsers.builtin._document_fixtures import package_parts, zip_bytes
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    metadata_map,
    source_for,
)

from structuraguard.contracts import ExtensionLocation, ExtractedBatch
from structuraguard.exceptions import ParserError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    DocxParser,
    LogParser,
    MarkdownParser,
    PlainTextParser,
    builtin_delimited_parsers,
    builtin_document_parsers,
    builtin_json_parsers,
    builtin_markup_parsers,
    builtin_text_parsers,
)
from structuraguard.ports import Parser

pytestmark = pytest.mark.anyio


def _core_registry(*, reverse: bool = False) -> ParserRegistry:
    parsers = (
        *builtin_text_parsers(),
        *builtin_delimited_parsers(),
        *builtin_json_parsers(),
        *builtin_markup_parsers(),
        *builtin_document_parsers(),
    )
    registry = ParserRegistry()
    registry.register_many(reversed(parsers) if reverse else parsers)
    return registry


def _docx(body: str) -> bytes:
    parts = package_parts("docx")
    parts["word/document.xml"] = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>"
    ).encode()
    return zip_bytes(parts)


@pytest.mark.parametrize("batch_size", (1, 2))
async def test_docx_preserves_hyphen_characters_and_run_provenance(
    batch_size: int,
) -> None:
    content = _docx(
        "<w:p><w:r><w:t>AA</w:t><w:noBreakHyphen/><w:t>BB</w:t>"
        "<w:softHyphen/><w:t>CC</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>tail</w:t></w:r></w:p>"
    )
    source = source_for(content, display_name="hyphens.docx")
    probe, parse = contexts_for(source, content, batch_size=batch_size)
    registry = ParserRegistry()
    registry.register(DocxParser())
    async with registry.session() as session:
        selected = await session.select(source, probe)
        batches = tuple([batch async for batch in selected.parse(source, parse)])
    manifest = batches[-1].manifest
    assert manifest is not None
    manifest.validate_batches(batches)
    blocks = [block for batch in batches for block in batch.blocks]
    assert [block.text for block in blocks] == ["AA\u2011BB\u00adCC", "tail"]
    for index, tag in ((1, "noBreakHyphen"), (3, "softHyphen")):
        location = blocks[0].values[index].location
        assert isinstance(location, ExtensionLocation)
        assert location.namespace == "docx:run"
        assert metadata_map(location.metadata)["tag"] == tag
        assert metadata_map(location.metadata)["child_index"] == index


@pytest.mark.parametrize(
    "body",
    (
        '<w:p><w:r><w:sym w:font="Symbol" w:char="F061"/></w:r></w:p>',
        "<w:tbl><w:tr><w:tc><w:sdt><w:sdtContent><w:p><w:r>"
        "<w:t>DO_NOT_DROP</w:t></w:r></w:p></w:sdtContent></w:sdt>"
        "</w:tc></w:tr></w:tbl>",
        "<w:tbl><w:tr><w:sdt><w:sdtContent><w:tc><w:p><w:r>"
        "<w:t>DO_NOT_DROP</w:t></w:r></w:p></w:tc></w:sdtContent>"
        "</w:sdt></w:tr></w:tbl>",
        "<w:tbl><w:sdt><w:sdtContent><w:tr><w:tc><w:p><w:r>"
        "<w:t>DO_NOT_DROP</w:t></w:r></w:p></w:tc></w:tr></w:sdtContent>"
        "</w:sdt></w:tbl>",
    ),
    ids=("run-symbol", "cell-control", "row-control", "table-control"),
)
async def test_docx_unsupported_content_never_produces_successful_manifest(
    body: str,
) -> None:
    content = _docx("<w:p><w:r><w:t>prefix</w:t></w:r></w:p>" + body)
    source = source_for(content, display_name="unsupported.docx")
    probe, parse = contexts_for(source, content, batch_size=1)
    registry = ParserRegistry()
    registry.register(DocxParser())
    emitted: list[ExtractedBatch] = []
    async with registry.session() as session:
        selected = await session.select(source, probe)
        stream = selected.parse(source, parse)
        with pytest.raises(ParserError) as failure:
            async for batch in stream:
                emitted.append(batch)
        assert not stream.completed
    assert failure.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert all(not batch.is_last and batch.manifest is None for batch in emitted)
    assert "DO_NOT_DROP" not in str(failure.value)


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32", "cp1251"))
@pytest.mark.parametrize("chunk_size", (1, 4096))
async def test_json_probes_do_not_block_text_encoding_in_core_registry(
    encoding: str, chunk_size: int
) -> None:
    text = "INFO " + "Это пример русского текста для определения кодировки. " * 5
    content = (text + "\n" + text + "\n").encode(encoding)
    source = source_for(content, display_name="misleading.json")
    reader = ShortReadSourceReader(
        source.source_fingerprint, content, max_chunk_bytes=chunk_size
    )
    probe, parse = contexts_for(source, content, reader=reader, batch_size=1)
    async with _core_registry().session() as session:
        selected = await session.select(source, probe)
        assert selected.adapter_id == "builtin.text"
        batches = [batch async for batch in selected.parse(source, parse)]
    assert [line.text for batch in batches for line in batch.lines] == [text, text]
    assert batches[-1].is_last


@pytest.mark.parametrize("reverse", (False, True))
@pytest.mark.parametrize(
    "content,expected,records",
    (
        (b'1\n{"a": 2}\n{"a": 3}\n', "builtin.json-lines", 3),
        (b'true\n{"a": 2}\nnull\n', "builtin.json-lines", 3),
        (b"name,value\nrow,alpha: beta\nrow2,gamma: delta\n", "builtin.delimited", 3),
        (b"#comment\nkey: [one, two]\n", "builtin.yaml", 1),
        (b"? [one, two]\n: value\n", "builtin.yaml", 1),
    ),
    ids=("scalar-jsonl", "literal-jsonl", "colon-csv", "comment-yaml", "complex-yaml"),
)
async def test_yaml_probe_only_claims_its_own_structure(
    content: bytes, expected: str, records: int, reverse: bool
) -> None:
    source = source_for(content, display_name="misleading.yaml")
    probe, parse = contexts_for(source, content, batch_size=1)
    async with _core_registry(reverse=reverse).session() as session:
        selected = await session.select(source, probe)
        assert selected.adapter_id == expected
        batches = [batch async for batch in selected.parse(source, parse)]
    assert sum(batch.record_count or 0 for batch in batches) == records
    manifest = batches[-1].manifest
    assert manifest is not None
    manifest.validate_batches(batches)


@pytest.mark.parametrize("parser", (PlainTextParser(), LogParser(), MarkdownParser()))
async def test_text_extraction_identity_includes_explicit_encoding(
    parser: Parser,
) -> None:
    content = b"\xe0\n"
    source = source_for(content, display_name="encoding.txt")
    context = contexts_for(source, content)[1]
    first = await collect(parser, source, replace(context, detected_encoding="cp1251"))
    second = await collect(parser, source, replace(context, detected_encoding="koi8-r"))
    repeated = await collect(
        parser, source, replace(context, detected_encoding="cp1251")
    )
    assert first == repeated
    assert first[0].lines[0].text == "а"
    assert second[0].lines[0].text == "Ю"
    assert first[0].extraction_id != second[0].extraction_id
    first_manifest, second_manifest = first[-1].manifest, second[-1].manifest
    assert first_manifest is not None and second_manifest is not None
    assert (
        first_manifest.parser_options_fingerprint
        != second_manifest.parser_options_fingerprint
    )
