"""Локализованный HTTPX transport без credentials в upstream diagnostics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.exceptions import LLMProviderError

if TYPE_CHECKING:
    import httpx


async def _redact_trace(event: str, info: dict[str, object]) -> None:
    # httpcore 1.0.9 вызывает trace до DEBUG serialization; started kwargs
    # участвуют в I/O, а completed/failed могут содержать headers/exception.
    if event.endswith((".complete", ".failed")):
        info.clear()


def safe_transport(inner: httpx.AsyncBaseTransport | None) -> httpx.AsyncBaseTransport:
    import httpx

    class SafeTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._inner = (
                inner
                if inner is not None
                else httpx.AsyncHTTPTransport(retries=0, trust_env=False)
            )

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            request.extensions["trace"] = _redact_trace
            response = await self._inner.handle_async_request(request)
            # HTTPX INFO пишет обе extensions до возврата client.send().
            response.extensions.pop("reason_phrase", None)
            response.extensions.pop("http_version", None)
            if sum(len(k) + len(v) for k, v in response.headers.raw) > 16384:
                await response.aclose()
                raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
            # Cookie jar long-lived client не должен накапливать upstream data.
            response.headers.pop("set-cookie", None)
            return response

        async def aclose(self) -> None:
            await self._inner.aclose()

    return SafeTransport()


async def read_bounded(response: httpx.Response, maximum: int) -> bytes:
    if sum(len(k) + len(v) for k, v in response.headers.raw) > 16384:
        raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
    lengths = response.headers.get_list("content-length")
    if lengths and (
        len(lengths) != 1
        or not lengths[0].isascii()
        or not lengths[0].isdigit()
        or len(lengths[0]) > 10
        or int(lengths[0]) > maximum
    ):
        raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
    # MockTransport может вернуть уже прочитанный body; real responses stream.
    if response.is_stream_consumed:
        body = response.content
        if len(body) > maximum:
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
        if lengths and len(body) != int(lengths[0]):
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
        return body
    chunks = bytearray()
    async for chunk in response.aiter_raw():
        if len(chunks) + len(chunk) > maximum:
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
        chunks.extend(chunk)
    if lengths and len(chunks) != int(lengths[0]):
        raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
    return bytes(chunks)
