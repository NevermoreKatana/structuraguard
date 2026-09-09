"""Real HTTP transport проверяется только локальным fake, без JVM/Tika service."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from tests.fakes.tika import RTF, XHTML
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import DataClassification
from structuraguard.parsers.tika import (
    TikaConfig,
    TikaEgressApproval,
    TikaParserAdapter,
)


@pytest.mark.anyio
@pytest.mark.integration
async def test_real_http_against_local_fake_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[bytes, bytes]] = []
    completed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.partition(b":")[2])
                for line in head.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body = await reader.readexactly(length)
            requests.append((head, body))
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/xml; charset=utf-8\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
            )
            for offset in range(0, len(XHTML), 11):
                chunk = XHTML[offset : offset + 11]
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            completed.set()

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    source = source_for(RTF, display_name="private-name.rtf")
    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = cast(tuple[str, int], server.sockets[0].getsockname())[1]
        parser = TikaParserAdapter(
            config=TikaConfig(
                enabled=True,
                endpoint=f"http://127.0.0.1:{port}/tika",
                expected_server_version="3.2.3",
                allowed_media_types=frozenset({"application/rtf"}),
            ),
            approval=TikaEgressApproval(
                source_fingerprint=source.source_fingerprint,
                classification=DataClassification.PUBLIC,
                secrets_checked=True,
                contains_secrets=False,
            ),
        )
        batches = await collect(
            parser, source, contexts_for(source, RTF, batch_size=1)[1]
        )
        await asyncio.wait_for(completed.wait(), timeout=5)
    assert len(requests) == 1 and requests[0][1] == RTF
    assert requests[0][0].startswith(b"PUT /tika HTTP/1.1\r\n")
    assert b"authorization:" not in requests[0][0].lower()
    assert source.display_name.encode() not in requests[0][0]
    assert batches[-1].manifest is not None
    batches[-1].manifest.validate_batches(batches)
