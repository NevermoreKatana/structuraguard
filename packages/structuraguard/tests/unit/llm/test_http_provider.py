"""HTTP boundary использует только scripted transport и фиксированные часы."""

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from tests.contract_suites.llm import (
    LLMProviderContractCase,
    assert_llm_provider_contract,
)
from tests.fakes.llm import digest, fixed_clock, request_for

from structuraguard.contracts import ProviderCapabilities
from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.llm import LLMErrorCode, LLMExecutionEnvironment
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import (
    LLMPromptTemplate,
    LLMResponseSchema,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)


class Result(FrozenContract):
    """Закрытая тестовая response schema."""

    value: str


def capabilities(*, native_schema: bool = True) -> ProviderCapabilities:
    return ProviderCapabilities(
        provider_id="test-http",
        provider_version="1.0.0",
        model_id="test-model",
        structured_output=True,
        json_schema=native_schema,
        supported_purposes=("semantic_parsing",),
        execution_environment=LLMExecutionEnvironment.LOCAL,
        max_input_bytes=65536,
        max_output_bytes=4096,
        max_input_tokens=8192,
        max_output_tokens=1024,
        context_window_tokens=16384,
    )


def provider_for(
    transport: httpx.AsyncBaseTransport, *, native_schema: bool = True
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=capabilities(native_schema=native_schema),
        ),
        prompts=(
            LLMPromptTemplate(
                prompt_id="semantic_parse_plan", version="1.0.0", text="prompt v1"
            ),
        ),
        schemas=(
            LLMResponseSchema(schema_id="test-result", version="1.0.0", model=Result),
        ),
        transport=transport,
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    )


def reply(content: str = '{"value":"ok"}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "test-model",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 32, "completion_tokens": 6},
        },
    )


@pytest.mark.anyio
@pytest.mark.parametrize("native_schema", [False, True])
async def test_http_runs_shared_contract_and_separates_untrusted_input(
    native_schema: bool,
) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply()

    provider = provider_for(httpx.MockTransport(handle), native_schema=native_schema)
    with pytest.raises(LLMProviderError, match="LLM_UNAVAILABLE"):
        await provider.generate_structured(request_for())
    async with provider:
        await assert_llm_provider_contract(
            LLMProviderContractCase(
                provider=provider,
                request=request_for(),
                expected_output='{"value":"ok"}',
            )
        )
    assert len(seen) == 1
    wire = json.loads(seen[0].content)
    assert wire["messages"][0]["content"] == "prompt v1"
    assert wire["messages"][-1]["role"] == "user"
    assert "masked" in wire["messages"][-1]["content"]
    assert wire["response_format"]["type"] == (
        "json_schema" if native_schema else "json_object"
    )
    assert "tools" not in wire and wire["stream"] is False
    assert provider.calls[0].prompt.fingerprint == digest("prompt v1")
    with pytest.raises(LLMProviderError, match="LLM_UNAVAILABLE"):
        await provider.generate_structured(request_for())


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status,code",
    [
        (429, LLMErrorCode.RATE_LIMIT),
        (503, LLMErrorCode.UNAVAILABLE),
        (401, LLMErrorCode.AUTHENTICATION_FAILED),
    ],
)
async def test_status_is_typed_without_raw_error_body(
    status: int, code: LLMErrorCode
) -> None:
    async with provider_for(
        httpx.MockTransport(lambda _: httpx.Response(status, text="secret-canary"))
    ) as provider:
        await assert_llm_provider_contract(
            LLMProviderContractCase(
                provider=provider, request=request_for(), expected_error=code
            )
        )
        assert "secret-canary" not in repr(provider.calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,code",
    [
        ("{", LLMErrorCode.INVALID_RESPONSE),
        ('{"value":1}', LLMErrorCode.SCHEMA_VIOLATION),
        ('{"value":"ok","extra":1}', LLMErrorCode.SCHEMA_VIOLATION),
    ],
)
async def test_malformed_and_schema_failures_are_not_repaired(
    content: str, code: LLMErrorCode
) -> None:
    async with provider_for(httpx.MockTransport(lambda _: reply(content))) as provider:
        with pytest.raises(LLMProviderError) as error:
            await provider.generate_structured(request_for())
    assert error.value.error_code == code.value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "exception,code",
    [
        (httpx.ReadTimeout("secret-canary"), LLMErrorCode.TIMEOUT),
        (httpx.ConnectError("secret-canary"), LLMErrorCode.UNAVAILABLE),
        (httpx.RemoteProtocolError("secret-canary"), LLMErrorCode.INVALID_RESPONSE),
    ],
)
async def test_transport_errors_are_normalized(
    exception: httpx.RequestError, code: LLMErrorCode
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise exception

    async with provider_for(httpx.MockTransport(handle)) as provider:
        with pytest.raises(LLMProviderError) as error:
            await provider.generate_structured(request_for())
        assert error.value.error_code == code.value
        assert "secret-canary" not in str(error.value)
        assert error.value.__suppress_context__
        assert provider.call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code,expected",
    [
        ("context_length_exceeded", LLMErrorCode.CONTEXT_LIMIT),
        ("unsupported_response_format", LLMErrorCode.CAPABILITY_MISMATCH),
    ],
)
async def test_upstream_context_and_capability_errors(
    code: str, expected: LLMErrorCode
) -> None:
    async with provider_for(
        httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"error": {"code": code, "message": "secret-canary"}}
            )
        )
    ) as provider:
        with pytest.raises(LLMProviderError) as error:
            await provider.generate_structured(request_for())
    assert error.value.error_code == expected.value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"model": "untrusted-model"},
        {"usage": None},
        {"usage": {"prompt_tokens": True, "completion_tokens": 1}},
        {"choices": []},
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"value":"ok"}',
                        "tool_calls": [{}],
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    ],
)
async def test_invalid_envelope_never_becomes_a_response(
    change: dict[str, object],
) -> None:
    envelope = json.loads(reply().content)
    envelope.update(change)
    async with provider_for(
        httpx.MockTransport(lambda _: httpx.Response(200, json=envelope))
    ) as provider:
        with pytest.raises(LLMProviderError, match="LLM_INVALID_RESPONSE"):
            await provider.generate_structured(request_for())


@pytest.mark.anyio
async def test_caller_cancel_and_close_have_no_retry_and_close_transport_once() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    class Transport(httpx.AsyncBaseTransport):
        closed = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            entered.set()
            await release.wait()
            return reply()

        async def aclose(self) -> None:
            self.closed += 1

    transport = Transport()
    provider = provider_for(transport)
    await provider.start()
    task = asyncio.create_task(provider.generate_structured(request_for()))
    await entered.wait()
    await asyncio.gather(provider.aclose(), provider.aclose())
    with pytest.raises(asyncio.CancelledError):
        await task
    await provider.aclose()
    assert transport.closed == 1
    assert provider.calls[0].outcome == "cancelled"
    assert provider.call_count == 1


@pytest.mark.anyio
async def test_stream_limit_closes_response_before_returning_failure() -> None:
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b" " * 100000
            pytest.fail("Чтение после превышения budget запрещено")

        async def aclose(self) -> None:
            self.closed = True

    stream = Stream()
    async with provider_for(
        httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
    ) as provider:
        with pytest.raises(LLMProviderError, match="LLM_INVALID_RESPONSE"):
            await provider.generate_structured(request_for())
    assert stream.closed


@pytest.mark.anyio
async def test_unknown_prompt_schema_and_context_are_denied_without_http() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("Preflight denial не должен отправлять HTTP")

    async with provider_for(httpx.MockTransport(handle)) as provider:
        request = request_for().model_copy(update={"response_schema_id": "unknown"})
        with pytest.raises(LLMProviderError, match="LLM_CAPABILITY_MISMATCH"):
            await provider.generate_structured(request)
        request = request_for('{"sample":"' + "a" * 9000 + '"}')
        with pytest.raises(LLMProviderError, match="LLM_CONTEXT_LIMIT"):
            await provider.generate_structured(request)
    assert provider.call_count == 0
