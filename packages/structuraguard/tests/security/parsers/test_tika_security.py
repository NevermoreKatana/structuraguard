"""Egress и hostile HTTP/XML regression corpus без внешнего Tika."""

from __future__ import annotations

import asyncio
import importlib.util
import traceback
from dataclasses import replace

import pytest
from tests.fakes.parsers import FakeSourceReader
from tests.fakes.tika import RTF, XHTML, FakeTikaServer, adapter_for
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import DataClassification
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import XmlParserLimits
from structuraguard.parsers.tika import (
    TikaConfig,
    TikaEgressApproval,
    TikaParserAdapter,
    TikaParserLimits,
)

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "policy",
    ["absent", "restricted", "internal", "unchecked", "secrets", "wrong-source"],
)
async def test_egress_requires_source_bound_secret_review(
    policy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    base, server = adapter_for(source, monkeypatch)
    assert base.approval is not None
    approval = base.approval
    if policy == "restricted":
        approval = TikaEgressApproval(source_fingerprint=source.source_fingerprint)
    elif policy == "internal":
        approval = replace(approval, classification=DataClassification.INTERNAL)
    elif policy == "unchecked":
        approval = replace(approval, secrets_checked=False)
    elif policy == "secrets":
        approval = replace(approval, contains_secrets=True)
    elif policy == "wrong-source":
        approval = replace(approval, source_fingerprint="sha256:" + "0" * 64)
    parser = TikaParserAdapter(
        config=base.config, approval=None if policy == "absent" else approval
    )
    reader = FakeSourceReader(source.source_fingerprint, content=RTF)
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED"):
        await collect(parser, source, contexts_for(source, RTF, reader=reader)[1])
    assert not server.requests and not reader.reads


@pytest.mark.parametrize(
    "marker",
    [
        b"password=secret-canary",
        b"Authorization: Bearer abc12345",
        b"-----BEGIN RSA PRIVATE KEY-----",
        b"postgres://user:secret-canary@db/path",
        b"api_key=secret-canary",
    ],
)
async def test_entire_request_is_scanned_before_first_upload(
    marker: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"{\\rtf1 " + b"x " * 6000 + marker + b"}"
    source = source_for(content, display_name="a.rtf")
    parser, server = adapter_for(
        source, monkeypatch, limits=TikaParserLimits(read_chunk_bytes=17)
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED") as failure:
        await collect(parser, source, contexts_for(source, content)[1])
    assert not server.requests
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))


async def test_reader_cannot_replace_approved_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(source, monkeypatch)
    changed = RTF.replace(b"fixture", b"changed")
    reader = FakeSourceReader(source.source_fingerprint, content=changed)
    with pytest.raises(SecurityPolicyError, match="SOURCE_FINGERPRINT_MISMATCH"):
        await collect(parser, source, contexts_for(source, RTF, reader=reader)[1])
    assert not server.requests


@pytest.mark.parametrize(
    "content",
    [b"%!PS-Adobe-3.0\npublic fixture\n", b"%PDF-1.7\npublic fixture\n"],
    ids=["disallowed-postscript", "core-pdf"],
)
@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
async def test_upload_rechecks_format_on_the_approved_snapshot(
    content: bytes, chunk_size: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(content, display_name="untrusted.rtf")
    base, server = adapter_for(source, monkeypatch)
    parser = TikaParserAdapter(
        config=replace(base.config, allowed_media_types=frozenset({"application/rtf"})),
        approval=base.approval,
        limits=TikaParserLimits(read_chunk_bytes=chunk_size),
    )

    class ChangedAfterProbe(FakeSourceReader):
        first_read = True

        async def read(self, *, offset: int, size: int) -> bytes:
            if self.first_read:
                self.first_read = False
                return RTF[:size]
            return await super().read(offset=offset, size=size)

    reader = ChangedAfterProbe(source.source_fingerprint, content=content)
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED"):
        await collect(parser, source, contexts_for(source, content, reader=reader)[1])
    assert not server.requests


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:secret-canary@host/tika",
        "https://host/tika?token=secret-canary",
        "https://host/tika#secret-canary",
        "https://host/secret-canary/tika",
        "file:///tika",
        "http://remote.invalid/tika",
        "http://localhost/tika",
        "http://127.0.0.1:0/tika",
        "https://host%40evil/tika",
        "https://host\r\n/tika",
        "https://host,evil/tika",
    ],
)
async def test_endpoint_rejects_credentials_and_unsafe_url(endpoint: str) -> None:
    with pytest.raises(ValueError) as failure:
        TikaConfig(endpoint=endpoint)
    assert "secret-canary" not in str(failure.value)


@pytest.mark.parametrize(
    "headers,status,code",
    [
        (
            {"location": "https://evil.invalid/secret-canary"},
            302,
            "SECURITY_INPUT_REJECTED",
        ),
        ({"content-type": "application/json"}, 200, "SECURITY_INPUT_REJECTED"),
        ({"content-type": "text/html"}, 200, "SECURITY_INPUT_REJECTED"),
        (
            {"content-type": "text/xml", "content-encoding": "gzip"},
            200,
            "SECURITY_INPUT_REJECTED",
        ),
        (
            {"content-type": "text/xml", "content-length": "9999999"},
            200,
            "SECURITY_LIMIT_EXCEEDED",
        ),
        (
            {"content-type": "text/xml", "content-length": "1"},
            200,
            "PARSER_MALFORMED_INPUT",
        ),
        ({"content-type": "text/xml; charset=latin1"}, 200, "PARSER_MALFORMED_INPUT"),
        (
            {"content-type": "text/xml", "x-extra": "x" * 9000},
            200,
            "SECURITY_LIMIT_EXCEEDED",
        ),
        ({"x-extra": "x" * 9000}, 204, "SECURITY_LIMIT_EXCEEDED"),
        ({"content-length": "100"}, 204, "PARSER_MALFORMED_INPUT"),
    ],
    ids=[
        "redirect",
        "mime",
        "html-serialization",
        "compression",
        "declared-size",
        "length-mismatch",
        "charset",
        "headers",
        "empty-headers",
        "empty-length",
    ],
)
async def test_hostile_response_headers(
    headers: dict[str, str], status: int, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(
        source, monkeypatch, server=FakeTikaServer(headers=headers, status=status)
    )
    with pytest.raises((SecurityPolicyError, ParserError), match=code):
        await collect(parser, source, contexts_for(source, RTF)[1])
    assert len(server.requests) == 1
    assert server.response_closed and server.closed


@pytest.mark.parametrize(
    "content,code",
    [
        (
            b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///secret-canary">]><html xmlns="http://www.w3.org/1999/xhtml">&x;</html>',
            "SECURITY_INPUT_REJECTED",
        ),
        (
            b'<!DOCTYPE html [<!ENTITY a "aa"><!ENTITY b "&a;&a;">]><html xmlns="http://www.w3.org/1999/xhtml">&b;</html>',
            "SECURITY_INPUT_REJECTED",
        ),
        (b"<html/>secret-canary", "PARSER_MALFORMED_INPUT"),
        (XHTML + b"\xff", "PARSER_MALFORMED_INPUT"),
        (
            b'<?xml version="1.0" encoding="ISO-8859-1"?>' + XHTML,
            "PARSER_MALFORMED_INPUT",
        ),
    ],
    ids=["xxe", "entities", "not-xhtml", "utf8", "encoding-conflict"],
)
async def test_malicious_or_malformed_xml_response(
    content: bytes, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(
        source, monkeypatch, server=FakeTikaServer(content=content)
    )
    with pytest.raises((SecurityPolicyError, ParserError), match=code) as failure:
        await collect(parser, source, contexts_for(source, RTF)[1])
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert server.response_closed and server.closed


@pytest.mark.parametrize("mode", ["timeout", "cancel", "disconnect"])
async def test_http_lifecycle_and_error_redaction(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    server = FakeTikaServer()
    if mode == "disconnect":
        server.fail_after_chunks = 2
    else:
        server.wait_for = asyncio.Event()
    parser, _ = adapter_for(
        source,
        monkeypatch,
        server=server,
        limits=TikaParserLimits(timeout_seconds=0.1 if mode == "timeout" else 10),
    )
    task = asyncio.create_task(collect(parser, source, contexts_for(source, RTF)[1]))
    await server.seen_request.wait()
    if mode == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(
            ParserError,
            match="PROCESSING_TIMEOUT"
            if mode == "timeout"
            else "PARSER_TIKA_UNAVAILABLE",
        ) as failure:
            await task
        assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert server.response_closed and server.closed
    assert len(server.requests) == 1


async def test_no_credentials_metadata_or_ambient_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret-canary@invalid:9")
    monkeypatch.setenv("NETRC", "/nonexistent-secret-canary")
    source = source_for(RTF, display_name="private-name.rtf")
    parser, server = adapter_for(
        source,
        monkeypatch,
        server=FakeTikaServer(
            headers={"content-type": "text/xml", "set-cookie": "token=secret-canary"}
        ),
    )
    for _ in range(2):
        await collect(parser, source, contexts_for(source, RTF)[1])
    for request in server.requests:
        assert not {
            "authorization",
            "proxy-authorization",
            "cookie",
            "content-disposition",
        } & set(request.headers)
        assert "secret-canary" not in str(request.headers) + str(request.url)
        assert source.display_name.encode() not in request.content
        assert request.headers["x-tika-skip-embedded"] == "true"
        assert request.headers["x-tika-pdfocrstrategy"] == "no_ocr"


async def test_missing_extra_and_disabled_do_not_read_or_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(source, monkeypatch)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _: None)
    with pytest.raises(ParserError, match="PARSER_DEPENDENCY_UNAVAILABLE"):
        await parser.probe(source, contexts_for(source, RTF)[0])
    disabled = TikaParserAdapter()
    reader = FakeSourceReader(source.source_fingerprint, content=RTF)
    probe, parse = contexts_for(source, RTF, reader=reader)
    assert not (await disabled.probe(source, probe)).supported
    with pytest.raises(ParserError, match="PARSER_UNSUPPORTED_FEATURE"):
        await collect(disabled, source, parse)
    assert not reader.reads and not server.requests


@pytest.mark.parametrize(
    "limits",
    [
        XmlParserLimits(max_nodes=2),
        XmlParserLimits(max_depth=2),
        XmlParserLimits(max_text_chars=3),
        XmlParserLimits(max_value_chars=3),
    ],
)
async def test_xhtml_resource_limits(
    limits: XmlParserLimits, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, _ = adapter_for(source, monkeypatch, limits=TikaParserLimits(xml=limits))
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await collect(parser, source, contexts_for(source, RTF)[1])
