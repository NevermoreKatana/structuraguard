"""Policy modes, общий резерв и fallback проверяются без сети и wall sleeps."""

import asyncio
from dataclasses import dataclass

import pytest
from tests.fakes.llm import digest, fixed_clock, request_for

from structuraguard.contracts import (
    DataClassification,
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    SecurityApproval,
    SecurityReport,
)
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.llm import (
    LLMBudget,
    LLMErrorCode,
    LLMExecutionEnvironment,
    LLMRoutePolicy,
    LLMRoutingMode,
    LLMRoutingPolicy,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import (
    FakeLLMProvider,
    PolicyAwareLLMRouter,
    ScriptedFailure,
    ScriptedResponse,
)


@dataclass
class Clock:
    seconds: float = 0.0

    def __call__(self) -> float:
        return self.seconds


class Deployment:
    """Fake transport с deployment metadata; cloud fixture также offline."""

    def __init__(
        self,
        name: str,
        *,
        environment: LLMExecutionEnvironment = LLMExecutionEnvironment.LOCAL,
        failure: bool = False,
    ) -> None:
        caps = FakeLLMProvider((), clock=fixed_clock).capabilities.model_copy(
            update={
                "provider_id": name,
                "model_id": name,
                "max_input_tokens": 100,
                "max_output_tokens": 20,
            }
        )
        self.fake = FakeLLMProvider(
            (
                ScriptedFailure(code=LLMErrorCode.RATE_LIMIT)
                if failure
                else ScriptedResponse(
                    output_json='{"value":"ok"}', input_tokens=10, output_tokens=2
                ),
            ),
            clock=fixed_clock,
            capabilities=caps,
        )
        self._caps = caps.model_copy(update={"execution_environment": environment})

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        return await self.fake.generate_structured(request)


def router_for(
    providers: tuple[Deployment, ...],
    *,
    mode: LLMRoutingMode = LLMRoutingMode.FALLBACK,
    budget: LLMBudget | None = None,
    clock: Clock | None = None,
    allowed: tuple[DataClassification, ...] = tuple(DataClassification),
) -> PolicyAwareLLMRouter:
    policy = LLMRoutingPolicy(
        policy_id="test-routing",
        mode=mode,
        routes=tuple(
            LLMRoutePolicy(
                capabilities_fingerprint=canonical_sha256_value(p.capabilities),
                allowed_classifications=allowed,
            )
            for p in providers
        ),
        budget=budget or LLMBudget(max_calls=4, max_tokens=1000, max_time_ms=10000),
    )
    return PolicyAwareLLMRouter(
        policy=policy,
        providers=providers,
        run_id="run-1",
        clock=fixed_clock,
        monotonic=clock or Clock(),
    )


def approved_request(
    router: PolicyAwareLLMRouter,
    classification: DataClassification = DataClassification.INTERNAL,
) -> LLMRequest:
    original = request_for()
    report = SecurityReport.model_validate(
        {
            **original.security_approval.report.model_dump(),
            "routing_policy_id": router.policy.policy_id,
            "routing_policy_fingerprint": router.policy_fingerprint,
            "data_classification": classification,
        }
    )
    return LLMRequest.model_validate(
        {
            **original.model_dump(),
            "routing_policy_id": report.routing_policy_id,
            "routing_policy_fingerprint": report.routing_policy_fingerprint,
            "data_classification": classification,
            "security_approval": SecurityApproval(
                report=report, report_fingerprint=digest(report.canonical_json())
            ),
        }
    )


@pytest.mark.anyio
async def test_fallback_has_shared_reservation_and_safe_attempt_metadata() -> None:
    primary, backup = Deployment("first", failure=True), Deployment("second")
    router = router_for((primary, backup))
    request = approved_request(router)
    result = await router.generate_structured(request)
    assert result.provider_id == "second"
    assert router.reserved_tokens == 240
    assert len(router.calls) == 2
    assert router.calls[0].error_code is LLMErrorCode.RATE_LIMIT
    assert router.calls[1].fallback_reason is LLMErrorCode.RATE_LIMIT
    assert router.calls[1].model_id == "second"
    assert router.calls[1].input_tokens == 10
    assert router.calls[1].elapsed_ms == 0
    assert {call.request_fingerprint for call in router.calls} == {
        canonical_sha256_value(request)
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode", [LLMRoutingMode.LOCAL_ONLY, LLMRoutingMode.PRIVACY_FIRST]
)
async def test_local_choice_precedes_cloud(mode: LLMRoutingMode) -> None:
    cloud, local = (
        Deployment("cloud", environment=LLMExecutionEnvironment.CLOUD),
        Deployment("local"),
    )
    router = router_for((cloud, local), mode=mode)
    assert (
        await router.generate_structured(approved_request(router))
    ).provider_id == "local"
    assert cloud.fake.call_count == 0


@pytest.mark.anyio
async def test_fixed_never_retries() -> None:
    primary = Deployment("primary", failure=True)
    router = router_for((primary,), mode=LLMRoutingMode.FIXED)
    with pytest.raises(LLMProviderError, match="LLM_RATE_LIMIT"):
        await router.generate_structured(approved_request(router))
    assert primary.fake.call_count == 1


@pytest.mark.anyio
async def test_no_llm_does_not_even_validate_payload() -> None:
    router = router_for((), mode=LLMRoutingMode.NO_LLM)
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED"):
        await router.generate_structured(LLMRequest.model_construct())
    assert router.calls == () and router.reserved_tokens == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "budget",
    [
        LLMBudget(max_calls=1, max_tokens=1000, max_time_ms=10000),
        LLMBudget(max_calls=4, max_tokens=239, max_time_ms=10000),
    ],
)
async def test_failed_attempt_does_not_refund_call_or_token_budget(
    budget: LLMBudget,
) -> None:
    primary, backup = Deployment("first", failure=True), Deployment("second")
    router = router_for((primary, backup), budget=budget)
    with pytest.raises(LLMProviderError, match="LLM_BUDGET_EXCEEDED"):
        await router.generate_structured(approved_request(router))
    assert primary.fake.call_count == 1 and backup.fake.call_count == 0
    assert router.reserved_tokens == 120


@pytest.mark.anyio
async def test_deadline_covers_idle_time_and_denies_before_egress() -> None:
    clock = Clock()
    primary = Deployment("primary")
    router = router_for((primary,), clock=clock)
    clock.seconds = 10
    with pytest.raises(LLMProviderError, match="LLM_BUDGET_EXCEEDED"):
        await router.generate_structured(approved_request(router))
    assert primary.fake.call_count == 0


@pytest.mark.anyio
async def test_cancellation_consumes_reservation_without_fallback() -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def checkpoint() -> None:
        entered.set()
        await release.wait()

    primary, backup = Deployment("primary"), Deployment("backup")
    primary.fake = FakeLLMProvider(
        (ScriptedResponse(output_json='{"value":"ok"}'),),
        clock=fixed_clock,
        capabilities=primary.capabilities,
        before_response=checkpoint,
    )
    router = router_for((primary, backup))
    request = approved_request(router)
    task = asyncio.create_task(router.generate_structured(request))
    await entered.wait()
    with pytest.raises(LLMProviderError, match="LLM_BUDGET_EXCEEDED"):
        await router.generate_structured(request)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert router.calls[0].outcome == "cancelled"
    assert router.reserved_tokens == 120
    assert backup.fake.call_count == 0
