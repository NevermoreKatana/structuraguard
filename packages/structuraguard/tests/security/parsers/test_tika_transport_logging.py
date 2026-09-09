"""Реальный HTTP boundary: upstream logs не должны раскрывать response secrets."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

import pytest
from tests.fakes.tika import RTF, XHTML
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import DataClassification
from structuraguard.exceptions import ParserError
from structuraguard.parsers.tika import (
    TikaConfig,
    TikaEgressApproval,
    TikaParserAdapter,
)


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize("malformed", [False, True], ids=["headers", "protocol-error"])
async def test_http_diagnostics_do_not_log_response_secrets(
    malformed: bool, caplog: pytest.LogCaptureFixture
) -> None:
    completed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            await reader.readexactly(len(RTF))
            if malformed:
                payload = b"HTTP/1.1 200 OK\r\nillegal-secret-canary\r\n\r\n"
            else:
                payload = (
                    b"HTTP/1.1 200 secret-canary\r\nContent-Type: text/xml\r\n"
                    b"Set-Cookie: session=secret-canary\r\nConnection: close\r\n"
                    + f"Content-Length: {len(XHTML)}\r\n\r\n".encode()
                    + XHTML
                )
            writer.write(payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            completed.set()

    source = source_for(RTF, display_name="public.rtf")
    logger = logging.getLogger("httpcore.http11")
    prior = (
        logger.level,
        logger.disabled,
        tuple(logger.filters),
        tuple(logger.handlers),
    )
    with caplog.at_level(logging.DEBUG):
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
            if malformed:
                with pytest.raises(ParserError, match="PARSER_MALFORMED_INPUT"):
                    await collect(parser, source, contexts_for(source, RTF)[1])
            else:
                batches = await collect(parser, source, contexts_for(source, RTF)[1])
                assert batches[-1].manifest is not None
            await asyncio.wait_for(completed.wait(), timeout=5)
    assert "secret-canary" not in caplog.text
    assert any(record.name.startswith("httpcore") for record in caplog.records)
    assert prior == (
        logger.level,
        logger.disabled,
        tuple(logger.filters),
        tuple(logger.handlers),
    )
