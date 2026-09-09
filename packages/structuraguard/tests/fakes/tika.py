"""Fake Tika HTTP server: bounded chunks, управляемая задержка, без сети/JAR."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest

from structuraguard.contracts import DataClassification, SourceArtifact
from structuraguard.parsers import _tika_http
from structuraguard.parsers.tika import (
    TikaConfig,
    TikaEgressApproval,
    TikaParserAdapter,
    TikaParserLimits,
)

XHTML = b'<?xml version="1.1" encoding="UTF-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml"><head><meta name="Author" content="raw"/></head><body><h1>Title</h1><p>001.20</p><ul><li>one</li></ul><table><tr><td>raw</td><td></td></tr></table></body></html>'
RTF = b"{\\rtf1 public fixture}"


class _ResponseStream(httpx.AsyncByteStream):
    def __init__(self, server: FakeTikaServer) -> None:
        self.server = server

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.server.wait_for is not None:
            await self.server.wait_for.wait()
        for offset in range(0, len(self.server.content), self.server.chunk_size):
            self.server.response_chunks += 1
            await asyncio.sleep(0)
            if self.server.fail_after_chunks == self.server.response_chunks:
                raise httpx.ReadError("secret-canary transport detail")
            yield self.server.content[offset : offset + self.server.chunk_size]

    async def aclose(self) -> None:
        self.server.response_closed = True


class FakeTikaServer(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        content: bytes = XHTML,
        headers: dict[str, str] | None = None,
        status: int = 200,
        chunk_size: int = 17,
    ) -> None:
        self.content, self.status, self.chunk_size = content, status, chunk_size
        self.headers = (
            headers
            if headers is not None
            else {"content-type": "text/xml; charset=UTF-8"}
        )
        self.requests: list[httpx.Request] = []
        self.response_chunks = 0
        self.response_closed = self.closed = False
        self.wait_for: asyncio.Event | None = None
        self.seen_request = asyncio.Event()
        self.fail_after_chunks: int | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.requests.append(request)
        self.seen_request.set()
        return httpx.Response(
            self.status, headers=self.headers, stream=_ResponseStream(self)
        )

    async def aclose(self) -> None:
        self.closed = True


def adapter_for(
    source: SourceArtifact,
    monkeypatch: pytest.MonkeyPatch,
    *,
    server: FakeTikaServer | None = None,
    limits: TikaParserLimits | None = None,
) -> tuple[TikaParserAdapter, FakeTikaServer]:
    selected_server = server if server is not None else FakeTikaServer()
    monkeypatch.setattr(_tika_http, "_transport", lambda: selected_server)
    adapter = TikaParserAdapter(
        config=TikaConfig(
            enabled=True,
            endpoint="http://127.0.0.1:9998/tika",
            allowed_media_types=frozenset(
                {"application/rtf", "application/postscript"}
            ),
            expected_server_version="3.2.3",
        ),
        approval=TikaEgressApproval(
            source_fingerprint=source.source_fingerprint,
            classification=DataClassification.PUBLIC,
            secrets_checked=True,
            contains_secrets=False,
        ),
        limits=limits,
    )
    return adapter, selected_server
