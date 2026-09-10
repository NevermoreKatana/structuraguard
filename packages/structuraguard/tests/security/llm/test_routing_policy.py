"""Privacy не ослабляется при fallback, forged approvals и caps drift."""

import pytest
from tests.unit.llm.test_router import Deployment, approved_request, router_for

from structuraguard.contracts import DataClassification
from structuraguard.contracts.llm import LLMExecutionEnvironment, LLMRoutingMode
from structuraguard.exceptions import LLMProviderError


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mode",
    [
        LLMRoutingMode.FIXED,
        LLMRoutingMode.FALLBACK,
        LLMRoutingMode.PRIVACY_FIRST,
        LLMRoutingMode.LOCAL_ONLY,
    ],
)
async def test_restricted_never_reaches_cloud_even_if_allowlisted(
    mode: LLMRoutingMode,
) -> None:
    cloud = Deployment("cloud", environment=LLMExecutionEnvironment.CLOUD)
    router = router_for((cloud,), mode=mode)
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED"):
        await router.generate_structured(
            approved_request(router, DataClassification.RESTRICTED)
        )
    assert cloud.fake.call_count == 0 and router.calls == ()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "classification", [DataClassification.CONFIDENTIAL, DataClassification.RESTRICTED]
)
async def test_privacy_fallback_cannot_escape_local(
    classification: DataClassification,
) -> None:
    local = Deployment("local", failure=True)
    cloud = Deployment("cloud", environment=LLMExecutionEnvironment.CLOUD)
    router = router_for((local, cloud), mode=LLMRoutingMode.PRIVACY_FIRST)
    with pytest.raises(LLMProviderError, match="LLM_RATE_LIMIT"):
        await router.generate_structured(approved_request(router, classification))
    assert local.fake.call_count == 1 and cloud.fake.call_count == 0


@pytest.mark.anyio
async def test_classification_allowlist_is_enforced() -> None:
    local = Deployment("local")
    router = router_for((local,), allowed=(DataClassification.PUBLIC,))
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED"):
        await router.generate_structured(approved_request(router))
    assert local.fake.call_count == 0


@pytest.mark.anyio
async def test_approval_for_other_policy_cannot_authorize_destination() -> None:
    first, second = Deployment("first"), Deployment("second")
    authorized = router_for((first,))
    changed = router_for((second,))
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED"):
        await changed.generate_structured(approved_request(authorized))
    assert second.fake.call_count == 0


@pytest.mark.anyio
async def test_caps_drift_denies_before_call() -> None:
    local = Deployment("local")
    router = router_for((local,))
    local._caps = local.capabilities.model_copy(
        update={"execution_environment": LLMExecutionEnvironment.CLOUD}
    )
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED"):
        await router.generate_structured(approved_request(router))
    assert local.fake.call_count == 0


@pytest.mark.anyio
async def test_forged_caps_do_not_emit_secret_serializer_warnings(
    recwarn: pytest.WarningsRecorder,
) -> None:
    local = Deployment("local")
    router = router_for((local,))
    local._caps = local.capabilities.model_copy(
        update={"max_input_tokens": "Bearer secret-canary"}
    )
    with pytest.raises(LLMProviderError, match="LLM_POLICY_DENIED") as error:
        await router.generate_structured(approved_request(router))
    assert "secret-canary" not in str(error.value)
    assert len(recwarn) == 0
    assert local.fake.call_count == 0
