"""Общий suite LLMProvider: DTO boundary, identity, lineage и явные отказы."""

from dataclasses import dataclass

import pytest

from structuraguard.contracts import LLMRequest, LLMResponse, ProviderCapabilities
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.exceptions import LLMProviderError
from structuraguard.ports import LLMProvider


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMProviderContractCase:
    """Сценарий, который каждый adapter запускает на контролируемом backend."""

    provider: LLMProvider
    request: LLMRequest
    expected_output: str | None = None
    expected_error: LLMErrorCode | None = None


async def assert_llm_provider_contract(case: LLMProviderContractCase) -> None:
    """Проверить одинаковую внешнюю boundary для success и disabled/failure."""
    assert isinstance(case.provider, LLMProvider)
    capabilities = case.provider.capabilities
    assert type(capabilities) is ProviderCapabilities
    assert (
        ProviderCapabilities.model_validate_json(capabilities.canonical_json())
        == capabilities
    )
    before = case.request.canonical_json()
    if case.expected_error is not None:
        with pytest.raises(LLMProviderError) as captured:
            await case.provider.generate_structured(case.request)
        assert captured.value.error_code == case.expected_error.value
        assert captured.value.retryable == (
            case.expected_error
            in {
                LLMErrorCode.TIMEOUT,
                LLMErrorCode.RATE_LIMIT,
                LLMErrorCode.UNAVAILABLE,
            }
        )
    else:
        result = await case.provider.generate_structured(case.request)
        assert type(result) is LLMResponse
        assert result.output_json == case.expected_output
        assert result.request_id == case.request.request_id
        assert (result.provider_id, result.provider_version, result.model_id) == (
            capabilities.provider_id,
            capabilities.provider_version,
            capabilities.model_id,
        )
        assert (result.response_schema_id, result.response_schema_version) == (
            case.request.response_schema_id,
            case.request.response_schema_version,
        )
        assert result.prompt == case.request.prompt
        assert result.prompt_fingerprint == case.request.prompt_fingerprint
        assert len(result.output_json.encode()) <= min(
            capabilities.max_output_bytes,
            case.request.max_output_bytes,
        )
        assert LLMResponse.model_validate_json(result.canonical_json()) == result
    assert case.request.canonical_json() == before
