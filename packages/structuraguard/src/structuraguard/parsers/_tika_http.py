"""Единственная opt-in HTTP граница Tika; нет JAR, окружения и retries."""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import tempfile
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from structuraguard.contracts.common import _contains_credential_canary
from structuraguard.contracts.source import SourceArtifact
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.ports.source import ParseContext

from .builtin._common import _read_checked, limit_error
from .builtin._markup import missing_extra, rejected

if TYPE_CHECKING:
    import httpx

    from .tika import TikaParserLimits


async def _redact_http_trace(event: str, info: dict[str, object]) -> None:
    # В закреплённом httpcore callback выполняется до DEBUG serialization.
    # started kwargs используются самим transport: их изменять нельзя.
    if event.endswith((".complete", ".failed")):
        info.clear()


def _transport() -> httpx.AsyncBaseTransport:
    import httpx

    class RedactedTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            request.extensions["trace"] = _redact_http_trace
            response = await super().handle_async_request(request)
            # HTTPX пишет reason phrase на INFO до возврата client.stream().
            # Удаляется только diagnostic extension, не проверяемые headers/body.
            response.extensions.pop("reason_phrase", None)
            return response

    return RedactedTransport(retries=0, trust_env=False)


def malformed_response() -> ParserError:
    return ParserError(
        error_code="PARSER_MALFORMED_INPUT",
        message="Некорректный ответ Tika.",
        details={"reason": "invalid_tika_response"},
    )


def _check(resource: str, value: int, maximum: int) -> None:
    if value > maximum:
        raise limit_error(adapter_id="optional.tika", resource=resource, limit=maximum)


def _response_headers(response: httpx.Response, limits: TikaParserLimits) -> None:
    _check(
        "response_header_bytes",
        sum(len(k) + len(v) for k, v in response.headers.raw),
        limits.max_header_bytes,
    )
    content_types = response.headers.get_list("content-type")
    if len(content_types) != 1:
        raise malformed_response()
    parts = [p.strip().lower() for p in content_types[0].split(";")]
    if parts[0] not in {"text/xml", "application/xhtml+xml"}:
        raise rejected("tika_response_content_type")
    if len(parts) > 2 or (
        len(parts) == 2 and parts[1] not in {"charset=utf-8", 'charset="utf-8"'}
    ):
        raise malformed_response()
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise rejected("tika_response_compression")
    lengths = response.headers.get_list("content-length")
    if lengths:
        if (
            len(lengths) != 1
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or len(lengths[0]) > 10
        ):
            raise malformed_response()
        _check("response_bytes", int(lengths[0]), limits.max_response_bytes)


async def exchange(
    source: SourceArtifact,
    context: ParseContext,
    *,
    endpoint: str,
    media_type: str,
    expected_signature: bytes,
    limits: TikaParserLimits,
) -> bytes | None:
    """Сначала проверить весь snapshot; ни одного байта egress до проверки SHA/секретов."""

    try:
        import httpx
        from defusedxml.ElementTree import DefusedXMLParser
    except ImportError:
        raise missing_extra("tika") from None
    if not callable(DefusedXMLParser):
        raise missing_extra("tika")
    _check(
        "request_bytes",
        source.size_bytes,
        min(limits.max_request_bytes, context.max_bytes),
    )
    if context.source_fingerprint != source.source_fingerprint:
        raise rejected("tika_source_identity")
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            with tempfile.TemporaryFile(mode="w+b") as spool:
                offset = 0
                digest = hashlib.sha256()
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                previous = ""
                while offset < source.size_bytes:
                    await asyncio.sleep(0)
                    chunk = await _read_checked(
                        context.reader,
                        offset=offset,
                        size=min(limits.read_chunk_bytes, source.size_bytes - offset),
                    )
                    if not chunk:
                        raise malformed_response()
                    decoded = previous + decoder.decode(chunk)
                    if _contains_credential_canary(decoded):
                        raise rejected("tika_source_credentials")
                    previous = decoded[-4096:]
                    digest.update(chunk)
                    spool.write(chunk)
                    offset += len(chunk)
                if "sha256:" + digest.hexdigest() != source.source_fingerprint:
                    raise SecurityPolicyError(
                        error_code="SOURCE_FINGERPRINT_MISMATCH",
                        message="Snapshot не соответствует разрешённому fingerprint.",
                    )
                spool.seek(0)
                # Probe читает reader отдельно: его signature не доказывает формат
                # snapshot, прошедшего SHA/DLP preflight (TOCTOU).
                if spool.read(len(expected_signature)) != expected_signature:
                    raise rejected("tika_snapshot_format")
                spool.seek(0)

                async def upload() -> AsyncIterator[bytes]:
                    while chunk := spool.read(limits.read_chunk_bytes):
                        await asyncio.sleep(0)
                        yield chunk

                # Новый client на transfer: cookies/credentials не переживают запрос.
                async with (
                    httpx.AsyncClient(
                        transport=_transport(),
                        trust_env=False,
                        follow_redirects=False,
                        timeout=limits.timeout_seconds,
                    ) as client,
                    client.stream(
                        "PUT",
                        endpoint,
                        content=upload(),
                        headers={
                            "Content-Type": media_type,
                            "Accept": "text/xml",
                            "Accept-Encoding": "identity",
                            "Content-Length": str(source.size_bytes),
                            "X-Tika-Skip-Embedded": "true",
                            "X-Tika-PDFOcrStrategy": "no_ocr",
                        },
                    ) as response,
                ):
                    _check(
                        "response_header_bytes",
                        sum(len(k) + len(v) for k, v in response.headers.raw),
                        limits.max_header_bytes,
                    )
                    if 300 <= response.status_code < 400:
                        raise rejected("tika_redirect")
                    if response.status_code == 204:
                        if response.headers.get("content-length", "0") != "0":
                            raise malformed_response()
                        return None
                    if response.status_code == 422:
                        raise malformed_response()
                    if response.status_code != 200:
                        raise ParserError(
                            error_code="PARSER_TIKA_UNAVAILABLE",
                            message="Tika endpoint недоступен.",
                        )
                    _response_headers(response, limits)
                    content = bytearray()
                    async for chunk in response.aiter_raw(
                        chunk_size=limits.read_chunk_bytes
                    ):
                        _check(
                            "response_bytes",
                            len(content) + len(chunk),
                            limits.max_response_bytes,
                        )
                        content.extend(chunk)
                    if "content-length" in response.headers and len(content) != int(
                        response.headers["content-length"]
                    ):
                        raise malformed_response()
                    return bytes(content)
    except (TimeoutError, httpx.TimeoutException):
        raise ParserError(
            error_code="PROCESSING_TIMEOUT", message="Истёк timeout Tika transfer."
        ) from None
    except httpx.RemoteProtocolError:
        raise malformed_response() from None
    except (httpx.RequestError, OSError):
        raise ParserError(
            error_code="PARSER_TIKA_UNAVAILABLE", message="Tika endpoint недоступен."
        ) from None
