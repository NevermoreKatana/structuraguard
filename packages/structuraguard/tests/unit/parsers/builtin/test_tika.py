"""Contract optional Tika adapter: только явно разрешённый fallback."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.fakes.tika import RTF, XHTML, FakeTikaServer, adapter_for
from tests.unit.parsers.builtin._support import (
    collect,
    contexts_for,
    metadata_map,
    source_for,
)

from structuraguard.contracts import ExtensionLocation, PhysicalNodeKind
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    builtin_document_parsers,
    builtin_text_parsers,
)
from structuraguard.parsers.tika import TikaParserAdapter, TikaParserLimits


def test_tika_is_disabled_by_default() -> None:
    assert not TikaParserAdapter().config.enabled
    assert all(
        not isinstance(parser, TikaParserAdapter)
        for parser in (*builtin_document_parsers(), *builtin_text_parsers())
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,name,media_type",
    [
        (RTF, "source.rtf", "application/rtf"),
        (
            b"%!PS-Adobe-3.0\n(public fixture) show",
            "source.ps",
            "application/postscript",
        ),
    ],
    ids=["rtf", "postscript"],
)
async def test_tika_contract_and_response_relative_tree(
    content: bytes,
    name: str,
    media_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_for(content, display_name=name, media_type=media_type)
    parser, server = adapter_for(source, monkeypatch)
    probe, parse = contexts_for(source, content, batch_size=1)
    batches = await assert_parser_contract(
        ParserContractCase(
            parser=parser, source=source, probe_context=probe, parse_context=parse
        )
    )
    assert len(server.requests) == 1
    assert server.requests[0].content == content
    assert server.requests[0].headers["content-type"] == media_type
    assert server.requests[0].headers["accept"] == "text/xml"
    assert "content-disposition" not in server.requests[0].headers
    assert server.closed and server.response_closed
    nodes = [n for b in batches for n in b.trees]
    assert any(n.raw_name == "{http://www.w3.org/1999/xhtml}table" for n in nodes)
    assert any(n.value and n.value.raw_value.value == "001.20" for n in nodes)
    for node in nodes:
        assert isinstance(node.location, ExtensionLocation)
        assert node.location.source == source.ref
        data = metadata_map(node.location.metadata)
        assert data["fidelity"] == "response_relative"
        assert data["server_version_configured"] == "3.2.3"
        assert str(data["response_fingerprint"]) != source.source_fingerprint
    assert any(n.node_kind == PhysicalNodeKind.ELEMENT and n.parent_id for n in nodes)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,name",
    [
        (b"hello", "a.txt"),
        (b'{"a":1}', "a.json"),
        (b"<x/>", "a.xml"),
        (b"<html/>", "a.html"),
        (b"key: value", "a.yaml"),
        (b"a,b\n1,2", "a.csv"),
        (b"PK\x03\x04", "a.xlsx"),
        (b"%PDF-1.7", "a.pdf"),
        (b"# head", "a.md"),
        (b"INFO a", "a.log"),
    ],
    ids=["txt", "json", "xml", "html", "yaml", "csv", "xlsx", "pdf", "md", "log"],
)
async def test_tika_does_not_claim_core_formats(
    content: bytes, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(content, display_name=name)
    parser, server = adapter_for(source, monkeypatch)
    assert not (await parser.probe(source, contexts_for(source, content)[0])).supported
    assert not server.requests


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,status",
    [(b"", 204), (b'<html xmlns="http://www.w3.org/1999/xhtml"/>', 200)],
    ids=["no-content", "empty-xhtml"],
)
async def test_tika_empty_response(
    content: bytes, status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, _ = adapter_for(
        source, monkeypatch, server=FakeTikaServer(content=content, status=status)
    )
    batches = await collect(parser, source, contexts_for(source, RTF)[1])
    assert batches[-1].manifest is not None
    batches[-1].manifest.validate_batches(batches)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code",
    [
        (503, "PARSER_TIKA_UNAVAILABLE"),
        (404, "PARSER_TIKA_UNAVAILABLE"),
        (422, "PARSER_MALFORMED_INPUT"),
    ],
)
async def test_tika_typed_outcomes_survive_registry(
    status: int, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, _ = adapter_for(source, monkeypatch, server=FakeTikaServer(status=status))
    probe, context = contexts_for(source, RTF)
    registry = ParserRegistry()
    registry.register(parser)
    async with registry.session() as session:
        selected = await session.select(source, probe)
        with pytest.raises(ParserError, match=code):
            async for _ in selected.parse(source, context):
                pass


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field_name,offset",
    [
        ("max_request_bytes", -1),
        ("max_request_bytes", 0),
        ("max_request_bytes", 1),
        ("max_response_bytes", -1),
        ("max_response_bytes", 0),
        ("max_response_bytes", 1),
    ],
)
async def test_tika_byte_boundaries(
    field_name: str, offset: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    size = len(RTF) if field_name == "max_request_bytes" else len(XHTML)
    limits = (
        TikaParserLimits(max_request_bytes=size + offset)
        if field_name == "max_request_bytes"
        else TikaParserLimits(max_response_bytes=size + offset)
    )
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(source, monkeypatch, limits=limits)
    if offset < 0:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await collect(parser, source, contexts_for(source, RTF)[1])
        if field_name == "max_request_bytes":
            assert not server.requests
    else:
        await collect(parser, source, contexts_for(source, RTF)[1])


@pytest.mark.anyio
async def test_tika_invalid_xml_is_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(
        source, monkeypatch, server=FakeTikaServer(content=XHTML + b"<broken>")
    )
    stream = parser.parse(source, contexts_for(source, RTF, batch_size=1)[1])
    assert isinstance(stream, AsyncGenerator)
    with pytest.raises(ParserError, match="PARSER_MALFORMED_INPUT"):
        await anext(stream)
    assert server.closed and server.response_closed
