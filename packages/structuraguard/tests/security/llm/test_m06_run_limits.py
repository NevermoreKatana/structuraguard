"""Run boundary не полагается на выполнение token contract сторонним adapter."""

import pytest
from tests.fakes.llm import fixed_clock, request_for
from tests.unit.llm.test_router import Deployment

from structuraguard.contracts import LLMRequest, LLMResponse
from structuraguard.contracts.llm import LLMBudget, LLMErrorCode
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm.run import LLMRunProvider

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "unknown", ("max_input_tokens", "max_output_tokens", "context_window_tokens")
)
async def test_unknown_token_cap_denies_run_before_provider(unknown: str) -> None:
    provider = Deployment("unknown")
    provider._caps = provider.capabilities.model_copy(update={unknown: None})
    run = LLMRunProvider(
        provider,
        LLMBudget(max_calls=1, max_tokens=1000, max_time_ms=1000),
        clock=fixed_clock,
        monotonic=lambda: 0,
    )
    with pytest.raises(LLMProviderError, match="LLM_CAPABILITY_MISMATCH"):
        await run.generate_structured(request_for())
    assert provider.fake.call_count == 0 and run.calls == ()


async def test_combined_usage_cannot_exceed_context_window() -> None:
    class ExcessUsage(Deployment):
        async def generate_structured(self, request: LLMRequest) -> LLMResponse:
            result = await super().generate_structured(request)
            return result.model_copy(update={"input_tokens": 80, "output_tokens": 20})

    provider = ExcessUsage("usage")
    provider._caps = provider.capabilities.model_copy(
        update={
            "max_input_tokens": 80,
            "max_output_tokens": 20,
            "context_window_tokens": 90,
        }
    )
    run = LLMRunProvider(
        provider,
        LLMBudget(max_calls=1, max_tokens=1000, max_time_ms=1000),
        clock=fixed_clock,
        monotonic=lambda: 0,
    )
    with pytest.raises(LLMProviderError, match="LLM_CONTEXT_LIMIT"):
        await run.generate_structured(request_for())
    assert provider.fake.call_count == 1
    assert run.calls[0].error_code == LLMErrorCode.CONTEXT_LIMIT
    assert run.calls[0].generation_fingerprint is None
