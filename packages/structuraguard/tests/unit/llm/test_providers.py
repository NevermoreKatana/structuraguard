"""Scripted providers: отсутствие сети/реального времени, последовательность и отмена."""

import asyncio
from datetime import timedelta
from typing import Literal

import pytest
from tests.contract_suites.llm import (
    LLMProviderContractCase,
    assert_llm_provider_contract,
)
from tests.fakes.llm import NOW, digest, fixed_clock, request_for

from structuraguard.contracts.llm import LLMErrorCode, LLMExecutionEnvironment
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import (
    FakeLLMProvider,
    NoLLMProvider,
    ScriptedFailure,
    ScriptedResponse,
)
from structuraguard.ports import LLMProvider


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [None, LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT, LLMErrorCode.UNAVAILABLE],
)
async def test_shared_contract_for_scripted_outcomes(
    code: Literal[
        LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT, LLMErrorCode.UNAVAILABLE
    ]
    | None,
) -> None:
    step = (
        ScriptedResponse(output_json='{"fields":[]}')
        if code is None
        else ScriptedFailure(code=code)
    )
    provider = FakeLLMProvider((step,), clock=fixed_clock)
    await assert_llm_provider_contract(
        LLMProviderContractCase(
            provider=provider,
            request=request_for(),
            expected_output='{"fields":[]}',
            expected_error=code,
        )
    )
    assert provider.call_count == 1
    assert provider.remaining_steps == 0
    assert len(provider.calls) == 1


@pytest.mark.anyio
async def test_disabled_provider_runs_same_contract_without_usable_capability() -> None:
    provider: LLMProvider = NoLLMProvider()
    await assert_llm_provider_contract(
        LLMProviderContractCase(
            provider=provider,
            request=request_for(),
            expected_error=LLMErrorCode.POLICY_DENIED,
        )
    )
    assert (
        provider.capabilities.execution_environment is LLMExecutionEnvironment.DISABLED
    )
    assert not provider.capabilities.structured_output
    assert not provider.capabilities.supported_purposes
    assert (
        provider.capabilities.max_input_bytes
        == provider.capabilities.max_output_bytes
        == 0
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    ["{", '{"x":1,"x":2}', '{"x":NaN}', "[1]", '{"x":1} trailing', '{ "x": 1 }'],
)
async def test_malformed_response_is_typed_and_not_repaired(payload: str) -> None:
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=payload),), clock=fixed_clock
    )
    await assert_llm_provider_contract(
        LLMProviderContractCase(
            provider=provider,
            request=request_for(),
            expected_error=LLMErrorCode.INVALID_RESPONSE,
        )
    )
    assert provider.calls[0].outcome == "failed"


@pytest.mark.anyio
async def test_script_order_usage_and_prompt_are_reproducible() -> None:
    script = (
        ScriptedFailure(code=LLMErrorCode.RATE_LIMIT, elapsed_ms=7),
        ScriptedResponse(
            output_json='{"ok":true}', input_tokens=12, output_tokens=4, elapsed_ms=9
        ),
    )
    first, second = (FakeLLMProvider(script, clock=fixed_clock) for _ in range(2))
    request = request_for()
    responses = []
    for provider in (first, second):
        with pytest.raises(LLMProviderError):
            await provider.generate_structured(request)
        responses.append(await provider.generate_structured(request))
        with pytest.raises(LLMProviderError, match="LLM_SCRIPT_EXHAUSTED"):
            await provider.generate_structured(request)
    assert responses[0] == responses[1]
    assert first.calls == second.calls
    assert responses[0].generated_at == NOW + timedelta(milliseconds=9)
    assert responses[0].prompt == request.prompt
    assert (responses[0].input_tokens, responses[0].output_tokens) == (12, 4)
    assert first.calls[-1].generation_fingerprint == digest('{"ok":true}')
    assert first.call_count == 2


@pytest.mark.anyio
async def test_unexpected_request_does_not_consume_script() -> None:
    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json="{}",
                expected_request_fingerprint=digest("different"),
            ),
        ),
        clock=fixed_clock,
    )
    with pytest.raises(LLMProviderError, match="LLM_SCRIPT_MISMATCH"):
        await provider.generate_structured(request_for())
    assert provider.remaining_steps == 1
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_cancelled_attempt_is_recorded_without_sleep_or_retry() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause() -> None:
        entered.set()
        await release.wait()

    provider = FakeLLMProvider(
        (ScriptedResponse(output_json="{}"),), clock=fixed_clock, before_response=pause
    )
    task = asyncio.create_task(provider.generate_structured(request_for()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.call_count == 1
    assert provider.calls[0].outcome == "cancelled"
    assert provider.remaining_steps == 0


@pytest.mark.anyio
async def test_history_is_bounded_and_instances_do_not_share_state() -> None:
    script = (ScriptedResponse(output_json="{}"),) * 3
    first = FakeLLMProvider(script, clock=fixed_clock, max_history=2)
    second = FakeLLMProvider(script, clock=fixed_clock)
    for _ in script:
        await first.generate_structured(request_for())
    assert [call.attempt for call in first.calls] == [2, 3]
    assert first.call_count == 3
    assert second.call_count == 0
    assert second.calls == ()


@pytest.mark.anyio
async def test_concurrent_calls_reserve_different_steps_before_checkpoint() -> None:
    ready, release = asyncio.Event(), asyncio.Event()
    entered = 0

    async def checkpoint() -> None:
        nonlocal entered
        entered += 1
        if entered == 2:
            ready.set()
        await release.wait()

    provider = FakeLLMProvider(
        (
            ScriptedResponse(output_json='{"n":1}'),
            ScriptedResponse(output_json='{"n":2}'),
        ),
        clock=fixed_clock,
        before_response=checkpoint,
    )
    first = asyncio.create_task(provider.generate_structured(request_for()))
    second = asyncio.create_task(provider.generate_structured(request_for()))
    await ready.wait()
    assert provider.call_count == 2
    release.set()
    responses = await asyncio.gather(first, second)
    assert [response.output_json for response in responses] == ['{"n":1}', '{"n":2}']
    assert {call.attempt for call in provider.calls} == {1, 2}


@pytest.mark.parametrize("max_history", [0, -1, True, 1001])
def test_history_configuration_is_bounded(max_history: int) -> None:
    with pytest.raises(ValueError):
        FakeLLMProvider((), clock=fixed_clock, max_history=max_history)


def test_script_count_and_total_bytes_are_bounded() -> None:
    with pytest.raises(ValueError, match="1000"):
        FakeLLMProvider((ScriptedResponse(output_json="{}"),) * 1001, clock=fixed_clock)
    step = ScriptedResponse(output_json="x" * 1_048_576)
    FakeLLMProvider((step,) * 4, clock=fixed_clock)
    with pytest.raises(ValueError, match="byte budget"):
        FakeLLMProvider((step,) * 5, clock=fixed_clock)
