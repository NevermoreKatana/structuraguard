"""Недоверенные adapter errors не попадают в публичные traceback и audit."""

import traceback
from collections.abc import AsyncIterator

import httpx
import pytest
from tests.fakes.llm import fixed_clock, request_for
from tests.fakes.semantic import Scanner, Validator, context, scenario
from tests.unit.llm.test_http_provider import provider_for
from tests.unit.llm.test_router import Deployment, approved_request, router_for
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import LLMRequest, LLMResponse
from structuraguard.contracts.llm import LLMBudget, LLMErrorCode
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm.run import LLMRunProvider
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.structure import LLMStructureAnalyzer

pytestmark = pytest.mark.anyio
CANARY = "upstream-secret-canary-m06"


class FailingDeployment(Deployment):
    """Ошибка стороннего adapter с raw текстом недоверенного ответа."""

    def __init__(self, failure: str) -> None:
        super().__init__("failing")
        self.failure = failure

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        if self.failure == "unexpected":
            raise RuntimeError(CANARY)
        error = LLMProviderError(LLMErrorCode.RATE_LIMIT)
        error.add_note(CANARY)
        if self.failure == "unknown_code":
            error.error_code = CANARY
        raise error


@pytest.mark.parametrize("boundary", ("router", "run", "analyzer"))
@pytest.mark.parametrize("failure", ("unexpected", "typed_note", "unknown_code"))
async def test_foreign_provider_errors_are_sanitized_at_each_public_boundary(
    boundary: str,
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FailingDeployment(failure)
    request, batches, _ = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    validator = Validator()
    router = router_for((provider,))
    run = LLMRunProvider(
        provider,
        LLMBudget(max_calls=2, max_tokens=1000, max_time_ms=1000),
        clock=fixed_clock,
        monotonic=lambda: 0,
    )
    with pytest.raises(LLMProviderError) as raised:
        if boundary == "router":
            await router.generate_structured(approved_request(router))
        elif boundary == "run":
            await run.generate_structured(request_for())
        else:
            await LLMStructureAnalyzer(
                provider=provider,
                scanner=Scanner(),
                validator=validator,
                context=context(),
            ).analyze(request, replay=lambda: stream(batches))
    error = raised.value
    expected = {
        "unexpected": LLMErrorCode.UNAVAILABLE,
        "typed_note": LLMErrorCode.RATE_LIMIT,
        "unknown_code": LLMErrorCode.INVALID_RESPONSE,
    }[failure]
    assert error.error_code == expected
    assert (
        CANARY
        not in "".join(traceback.format_exception(error))
        + repr(error.details)
        + caplog.text
    )
    assert validator.calls == 0
    for call in (*router.calls, *run.calls):
        assert call.outcome == "failed" and call.error_code == expected
        assert call.generation_fingerprint is None
        assert CANARY not in call.canonical_json()


@pytest.mark.parametrize("failure", ("read", "cleanup"))
async def test_http_stream_errors_are_typed_and_do_not_leak(failure: str) -> None:
    closed = 0

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            if failure == "read":
                raise RuntimeError(CANARY)
            yield b"{}"

        async def aclose(self) -> None:
            nonlocal closed
            closed += 1
            if failure == "cleanup":
                raise RuntimeError(CANARY)

    async with provider_for(
        httpx.MockTransport(lambda request: httpx.Response(200, stream=BrokenStream()))
    ) as provider:
        with pytest.raises(LLMProviderError) as raised:
            await provider.generate_structured(request_for())
        assert CANARY not in "".join(traceback.format_exception(raised.value))
        assert raised.value.error_code == LLMErrorCode.UNAVAILABLE
        assert provider.calls[0].outcome == "failed"
        assert closed == 1


async def test_http_transport_shutdown_cannot_leak_foreign_exception() -> None:
    class BrokenTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise AssertionError("No request expected")

        async def aclose(self) -> None:
            raise ValueError(CANARY)

    provider = provider_for(BrokenTransport())
    await provider.start()
    with pytest.raises(LLMProviderError, match="LLM_UNAVAILABLE") as raised:
        await provider.aclose()
    assert CANARY not in "".join(traceback.format_exception(raised.value))
    assert provider.call_count == 0
