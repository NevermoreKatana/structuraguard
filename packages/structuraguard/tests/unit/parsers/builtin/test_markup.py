"""Контракт независимых XML/HTML/YAML adapters и физической иерархии."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    metadata_map,
    source_for,
)

from structuraguard.contracts import (
    CssSelectorLocation,
    ExtensionLocation,
    PhysicalNodeKind,
    XPathLocation,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import (
    HtmlParser,
    HtmlParserLimits,
    XmlParser,
    XmlParserLimits,
    YamlParser,
    YamlParserLimits,
)
from structuraguard.ports import Parser


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "filename"),
    [
        (XmlParser(), b'<r xmlns="urn:x" a="01">before<x/>after<x>2</x></r>', "a.xml"),
        (
            HtmlParser(),
            b"<h1>Hello</h1><ul><li>A</li></ul><table><tr><td>01</td></tr></table>",
            "a.html",
        ),
        (YamlParser(), b"key: [01, true, null]\nkey: {x: 2}\n", "a.yaml"),
    ],
)
async def test_markup_contract(parser: Parser, content: bytes, filename: str) -> None:
    source = source_for(content, display_name=filename)
    probe, parse = contexts_for(source, content, batch_size=1)
    batches = await assert_parser_contract(
        ParserContractCase(
            parser=parser, source=source, probe_context=probe, parse_context=parse
        )
    )
    assert any(batch.trees for batch in batches)
    assert all(node.node_kind is not None for batch in batches for node in batch.trees)


@pytest.mark.anyio
async def test_xml_exact_xpath_mixed_content_and_namespaces() -> None:
    content = b'<?keep raw?><r xmlns="urn:r" xmlns:a="urn:a" a:v="01">pre<a:x/>tail<a:x>raw&amp;end</a:x><!--comment--></r>'
    source = source_for(content, display_name="a.xml")
    batches = await collect(
        XmlParser(), source, contexts_for(source, content, batch_size=2)[1]
    )
    nodes = {n.node_id: n for b in batches for n in b.trees}
    children = [n for n in nodes.values() if n.raw_name == "{urn:a}x"]
    assert len(children) == 2
    assert all(isinstance(n.location, XPathLocation) for n in nodes.values())
    assert isinstance(children[0].location, XPathLocation)
    assert isinstance(children[1].location, XPathLocation)
    assert children[0].location.xpath.endswith(
        "*[local-name()='x' and namespace-uri()='urn:a'][1]"
    )
    assert children[1].location.xpath.endswith(
        "*[local-name()='x' and namespace-uri()='urn:a'][2]"
    )
    text = [n for n in nodes.values() if n.node_kind is PhysicalNodeKind.TEXT]
    assert [n.value.raw_value.value for n in text if n.value] == [
        "pre",
        "tail",
        "raw&end",
    ]
    assert isinstance(text[1].location, XPathLocation)
    assert text[1].location.xpath.endswith("/text()[2]")
    assert any(
        n.node_kind is PhysicalNodeKind.NAMESPACE and n.raw_name == "a"
        for n in nodes.values()
    )
    assert any(n.value and n.value.raw_value.value == "01" for n in nodes.values())


@pytest.mark.anyio
async def test_html_relations_duplicate_attrs_and_physical_projections() -> None:
    content = b'<H1 DATA-X="01" data-x="02" disabled>Title</H1>\n<ul><li>A</li><li>B</li></ul><table><tr><th colspan="2">X</th></tr><tr><td></td><td>01</td></tr></table>'
    source = source_for(content, display_name="a.html")
    batches = await collect(
        HtmlParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    nodes = {n.node_id: n for b in batches for n in b.trees}
    attrs = [n for n in nodes.values() if n.node_kind is PhysicalNodeKind.ATTRIBUTE]
    assert [n.value.raw_value.value for n in attrs if n.value][:3] == ["01", "02", None]
    assert attrs[0].parent_id == attrs[1].parent_id
    assert attrs[0].location != attrs[1].location
    assert isinstance(attrs[0].location, CssSelectorLocation)
    assert attrs[0].location.line_number == 1
    assert attrs[0].location.selector == ":scope > *:nth-child(1)"
    table = next(t for b in batches for t in b.tables)
    assert [
        (c.row_index, c.column_index, c.value.raw_value.value) for c in table.cells
    ] == [(0, 0, "X"), (1, 0, ""), (1, 1, "01")]
    assert any(
        b.kind.value == "heading" and b.text == "Title"
        for batch in batches
        for b in batch.blocks
    )
    assert any(
        b.kind.value == "list" and b.text == "AB"
        for batch in batches
        for b in batch.blocks
    )
    assert any(n.raw_lexeme and 'DATA-X="01"' in n.raw_lexeme for n in nodes.values())


@pytest.mark.anyio
async def test_yaml_duplicate_complex_keys_scalars_aliases_and_marks() -> None:
    content = b"---\na: &first [01, true, null]\na: *first\n? [x, y]\n: z\n---\nself: &self [*self]\n"
    source = source_for(content, display_name="a.yaml")
    batches = await collect(
        YamlParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    nodes = [n for b in batches for n in b.trees]
    values = [n.value.raw_value.value for n in nodes if n.value]
    assert values[:6] == ["a", "01", "true", "null", "a", "first"]
    assert len([n for n in nodes if n.node_kind is PhysicalNodeKind.ALIAS]) == 2
    assert len([n for n in nodes if n.node_kind is PhysicalNodeKind.MAPPING]) == 2
    scalar = next(n for n in nodes if n.value and n.value.raw_value.value == "01")
    assert scalar.raw_lexeme == "01"
    assert isinstance(scalar.location, ExtensionLocation)
    assert metadata_map(scalar.location.metadata) == {
        "document_index": 0,
        "path": "/1/0",
        "line_start": 2,
        "column_start": 11,
        "line_end": 2,
        "column_end": 13,
    }
    second = next(n for n in nodes if n.value and n.value.raw_value.value == "self")
    assert isinstance(second.location, ExtensionLocation)
    assert metadata_map(second.location.metadata)["line_start"] == 7
    assert metadata_map(second.location.metadata)["document_index"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "line"),
    [
        (XmlParser(), b"<r>\n<x></r>", 2),
        (YamlParser(), b"a: 1\nb: [x\n", 3),
        (YamlParser(), b"---\na: 1\n---\nb: [x\n", 5),
        (YamlParser(), b"a: *missing\n", 1),
        (YamlParser(), b"a: &x 1\nb: &x 2\n", 2),
    ],
)
async def test_malformed_reports_exact_line(
    parser: Parser, content: bytes, line: int
) -> None:
    source = source_for(content, display_name="a.txt")
    with pytest.raises(ParserError) as failure:
        await collect(parser, source, contexts_for(source, content)[1])
    assert failure.value.error_code == "PARSER_MALFORMED_INPUT"
    assert failure.value.details["line_number"] == line
    assert "missing" not in str(failure.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (XmlParser(limits=XmlParserLimits(max_depth=1)), b"<r><x/></r>"),
        (XmlParser(limits=XmlParserLimits(max_nodes=2)), b"<r><x/><y/></r>"),
        (XmlParser(limits=XmlParserLimits(max_text_chars=3)), b"<r>four</r>"),
        (XmlParser(limits=XmlParserLimits(max_value_chars=3)), b'<r a="four"/>'),
        (
            XmlParser(limits=XmlParserLimits(max_attributes_per_node=1)),
            b'<r a="1" b="2"/>',
        ),
        (
            XmlParser(limits=XmlParserLimits(max_namespaces=1)),
            b'<r xmlns:a="a" xmlns:b="b"/>',
        ),
        (
            XmlParser(limits=XmlParserLimits(max_subtree_nodes=1)),
            b"<r><x>value</x></r>",
        ),
        (HtmlParser(limits=HtmlParserLimits(max_depth=1)), b"<div><p>x</p></div>"),
        (
            HtmlParser(limits=HtmlParserLimits(max_attributes_per_node=1)),
            b'<p a="1" b="2">',
        ),
        (HtmlParser(limits=HtmlParserLimits(max_value_chars=3)), b"<p>four</p>"),
        (YamlParser(limits=YamlParserLimits(max_depth=1)), b"a: [x]"),
        (YamlParser(limits=YamlParserLimits(max_nodes=2)), b"a: value"),
        (YamlParser(limits=YamlParserLimits(max_value_chars=3)), b"a: four"),
        (YamlParser(limits=YamlParserLimits(max_document_chars=5)), b"a: value"),
        (YamlParser(limits=YamlParserLimits(max_documents=1)), b"---\na\n---\nb\n"),
        (YamlParser(limits=YamlParserLimits(max_aliases=0)), b"a: &x value\nb: *x"),
        (YamlParser(limits=YamlParserLimits(max_anchors=0)), b"a: &x value"),
    ],
)
async def test_markup_limits_before_unbounded_accumulation(
    parser: Parser, content: bytes
) -> None:
    source = source_for(content, display_name="a.txt")
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await collect(parser, source, contexts_for(source, content)[1])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (XmlParser(limits=XmlParserLimits(read_chunk_bytes=1)), b"<r><x/><y/></r>"),
        (HtmlParser(limits=HtmlParserLimits(read_chunk_bytes=1)), b"<p>x</p><p>y</p>"),
        (YamlParser(limits=YamlParserLimits(read_chunk_bytes=1)), b"---\nx\n---\ny\n"),
    ],
)
async def test_context_limits_and_cancellation(parser: Parser, content: bytes) -> None:
    source = source_for(content, display_name="a.txt")
    context = contexts_for(source, content, batch_size=1)[1]
    for limited in (
        replace(context, max_bytes=len(content) - 1),
        replace(context, max_records=1),
        replace(context, max_physical_objects=1),
    ):
        with pytest.raises(SecurityPolicyError):
            await collect(parser, source, limited)
    stream = parser.parse(source, context)
    first = await anext(stream)
    assert not first.is_last
    task = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (XmlParser(), "<r>я😀</r>".encode("utf-16")),
        (HtmlParser(), "<p>я😀</p>".encode()),
        (YamlParser(), "a: я😀\n".encode()),
    ],
)
async def test_short_reads_preserve_multibyte(parser: Parser, content: bytes) -> None:
    source = source_for(content, display_name="a.txt")
    reader = ShortReadSourceReader(
        source.source_fingerprint, content, max_chunk_bytes=1
    )
    batches = await collect(
        parser, source, contexts_for(source, content, reader=reader)[1]
    )
    assert any(
        n.value and n.value.raw_value.value == "я😀" for b in batches for n in b.trees
    )


@pytest.mark.anyio
@pytest.mark.parametrize("parser", [HtmlParser(), YamlParser()])
async def test_utf8_decode_is_strict(parser: Parser) -> None:
    content = b"\xffinvalid"
    source = source_for(content, display_name="a.txt")
    with pytest.raises(ParserError, match="PARSER_ENCODING_UNSUPPORTED"):
        await collect(parser, source, contexts_for(source, content)[1])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (XmlParser(), b"<r><x/></r>"),
        (HtmlParser(), b"<p>x</p>"),
        (YamlParser(), b"a: value"),
    ],
)
async def test_one_exact_batch_fits_max_batches_one(
    parser: Parser, content: bytes
) -> None:
    source = source_for(content, display_name="a.txt")
    batches = await collect(
        parser, source, contexts_for(source, content, batch_size=1, max_batches=1)[1]
    )
    assert len(batches) == 1 and batches[0].is_last


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content", [b"---\na\n---", b"---\ra\r---", b"---\r\na\r\n---"]
)
async def test_yaml_final_empty_document_without_newline(content: bytes) -> None:
    source = source_for(content, display_name="a.yaml")
    batches = await collect(
        YamlParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    assert len(batches) == 2
    assert [n.value.raw_value.value for b in batches for n in b.trees if n.value] == [
        "a",
        "",
    ]


@pytest.mark.anyio
async def test_html_raw_references_without_semicolon_are_not_rewritten() -> None:
    content = b"<p>&amp x &#65 y &lt; z</p>"
    source = source_for(content, display_name="a.html")
    for size in (1, 4096):
        batches = await collect(
            HtmlParser(limits=HtmlParserLimits(read_chunk_bytes=size)),
            source,
            contexts_for(source, content)[1],
        )
        node = next(
            n for b in batches for n in b.trees if n.node_kind is PhysicalNodeKind.TEXT
        )
        assert node.value is not None and node.value.raw_value.value == "& x A y < z"
        assert node.raw_lexeme == "&amp x &#65 y &lt; z"


@pytest.mark.anyio
async def test_yaml_json_subset_declined_flow_yaml_and_bounded_prefix_supported() -> (
    None
):
    for content, expected in [
        (b'{"a": [1,true]}', False),
        (b"{a: [x, y]}", True),
        (b"[x, y]", True),
        (b"ordinary text", False),
        (b"a: x\n" * 20000, True),
    ]:
        source = source_for(content, display_name="misleading.txt")
        probe = contexts_for(source, content)[0]
        result = await YamlParser(limits=YamlParserLimits(max_probe_bytes=100)).probe(
            source, probe
        )
        assert result.supported is expected


@pytest.mark.anyio
async def test_xhtml_belongs_to_xml_and_html_doctype_to_html() -> None:
    for content, xml, html in [
        (b'<html xmlns="http://www.w3.org/1999/xhtml"><p>x</p></html>', True, False),
        (b"<!DOCTYPE html><html><p>x</p></html>", False, True),
        (b'<?xml version="1.0"?><html><p>x</p></html>', True, False),
    ]:
        source = source_for(content, display_name="misleading.txt")
        probe = contexts_for(source, content)[0]
        assert (await XmlParser().probe(source, probe)).supported is xml
        assert (await HtmlParser().probe(source, probe)).supported is html


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "module"),
    [
        (XmlParser(), b"<r/>", "defusedxml.ElementTree"),
        (YamlParser(), b"a: value", "yaml"),
    ],
)
async def test_missing_optional_backend_is_typed(
    parser: Parser, content: bytes, module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, module, None)
    source = source_for(content, display_name="a.txt")
    probe, parse = contexts_for(source, content)
    with pytest.raises(ParserError, match="PARSER_DEPENDENCY_UNAVAILABLE"):
        await parser.probe(source, probe)
    with pytest.raises(ParserError, match="PARSER_DEPENDENCY_UNAVAILABLE"):
        await collect(parser, source, parse)


@pytest.mark.anyio
async def test_xml_xpath_can_quote_namespace_with_both_quote_types() -> None:
    content = b'<r xmlns="urn:a&apos;&quot;b"><x/></r>'
    source = source_for(content, display_name="a.xml")
    batches = await collect(XmlParser(), source, contexts_for(source, content)[1])
    root = batches[0].trees[0]
    assert isinstance(root.location, XPathLocation)
    assert "namespace-uri()=concat(" in root.location.xpath


@pytest.mark.anyio
async def test_html_small_token_limit_accepts_many_small_complete_tags() -> None:
    content = b"<p>x</p>" * 100
    source = source_for(content, display_name="a.html")
    batches = await collect(
        HtmlParser(limits=HtmlParserLimits(max_token_chars=16)),
        source,
        contexts_for(source, content)[1],
    )
    assert sum(len(b.blocks) for b in batches) == 100


@pytest.mark.anyio
async def test_yaml_small_document_limit_accepts_many_documents_in_one_read() -> None:
    content = b"---\nx\n" * 100
    source = source_for(content, display_name="a.yaml")
    batches = await collect(
        YamlParser(limits=YamlParserLimits(max_document_chars=6)),
        source,
        contexts_for(source, content)[1],
    )
    assert sum(b.record_count or 0 for b in batches) == 100


@pytest.mark.anyio
async def test_yaml_directives_between_documents() -> None:
    content = b"%YAML 1.1\n---\na: 01\n...\n%YAML 1.2\n---\na: 02\n"
    source = source_for(content, display_name="a.yaml")
    batches = await collect(YamlParser(), source, contexts_for(source, content)[1])
    assert [n.value.raw_value.value for b in batches for n in b.trees if n.value] == [
        "a",
        "01",
        "a",
        "02",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "line"),
    [
        (YamlParser(), b"a: x\nb: \x00", 2),
        (YamlParser(), b'a: "\\uD800"', 1),
        (HtmlParser(), b"<p>\n<![bogus]>", 2),
    ],
)
async def test_backend_errors_never_escape_typed_boundary(
    parser: Parser, content: bytes, line: int
) -> None:
    source = source_for(content, display_name="a.txt")
    with pytest.raises(ParserError) as failure:
        await collect(parser, source, contexts_for(source, content)[1])
    assert failure.value.error_code == "PARSER_MALFORMED_INPUT"
    assert failure.value.details["line_number"] == line


@pytest.mark.anyio
async def test_xml_namespace_undeclaration_and_prolog_order_are_physical() -> None:
    content = b'<!--before--><r xmlns="urn:r"><x xmlns=""/></r><!--after-->'
    source = source_for(content, display_name="a.xml")
    batches = await collect(
        XmlParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    nodes = {n.node_id: n for b in batches for n in b.trees}
    roots = sorted(
        (n for n in nodes.values() if n.parent_id is None), key=lambda n: n.order
    )
    assert [n.node_kind for n in roots] == [
        PhysicalNodeKind.COMMENT,
        PhysicalNodeKind.ELEMENT,
        PhysicalNodeKind.COMMENT,
    ]
    undeclaration = next(
        n
        for n in nodes.values()
        if n.node_kind is PhysicalNodeKind.NAMESPACE
        and n.value
        and n.value.raw_value.value == ""
    )
    assert isinstance(undeclaration.location, XPathLocation)
    assert undeclaration.location.namespace_prefix == ""
    assert undeclaration.location.xpath.endswith("/x[1]")


@pytest.mark.anyio
async def test_html_declarations_remain_inert_physical_nodes() -> None:
    content = b"<!DOCTYPE   html><p><![CDATA[raw]]></p>"
    source = source_for(content, display_name="a.html")
    probe, context = contexts_for(source, content)
    assert (await HtmlParser().probe(source, probe)).supported
    assert not (await XmlParser().probe(source, probe)).supported
    batches = await collect(HtmlParser(), source, context)
    assert [
        n.value.raw_value.value
        for b in batches
        for n in b.trees
        if n.node_kind is PhysicalNodeKind.DECLARATION and n.value
    ] == ["DOCTYPE   html", "CDATA[raw"]


class _CountingReader(ShortReadSourceReader):
    read_end: int = 0

    async def read(self, *, offset: int, size: int) -> bytes:
        result = await super().read(offset=offset, size=size)
        self.read_end = offset + len(result)
        return result


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (XmlParser(), b"<r>" + b"<x/>" * 10000 + b"</r>"),
        (XmlParser(), b"<!--p-->" * 10000 + b"<r/>"),
        (HtmlParser(), b"<p>x</p>" * 10000),
        (YamlParser(), b"---\nx\n" * 10000),
    ],
)
async def test_first_batch_does_not_require_full_source(
    parser: Parser, content: bytes
) -> None:
    source = source_for(content, display_name="a.txt")
    reader = _CountingReader(source.source_fingerprint, content, max_chunk_bytes=16)
    stream = parser.parse(
        source, contexts_for(source, content, reader=reader, batch_size=1)[1]
    )
    assert not (await anext(stream)).is_last
    assert reader.read_end < 128
    # Закрываем consumer, не материализуя остаток большого snapshot.
    from collections.abc import AsyncGenerator

    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()
