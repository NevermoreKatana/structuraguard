"""Один semantic response contract для Fake, NoLLM и HTTP JSON modes."""

import json

import httpx
import pytest
from tests.contract_suites.llm import (
    LLMProviderContractCase,
    assert_llm_provider_contract,
)
from tests.fakes.llm import fixed_clock, request_for
from tests.fakes.semantic_mapping import fake_provider

from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.reports import LLMRequest
from structuraguard.contracts.semantic_mapping import SemanticMappingDecision
from structuraguard.llm import (
    NoLLMProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from structuraguard.mapping import (
    semantic_mapping_prompt,
    semantic_mapping_response_schema,
)

pytestmark = pytest.mark.anyio


def contract_request() -> LLMRequest:
    request = request_for()
    prompt = semantic_mapping_prompt().identity
    return LLMRequest.model_validate(
        {
            **request.model_dump(),
            "purpose": "semantic_mapping",
            "response_schema_id": "semantic-mapping-decision",
            "response_schema_version": "1.0.0",
            "prompt": prompt,
            "prompt_fingerprint": prompt.fingerprint,
        }
    )


def output() -> str:
    return SemanticMappingDecision(
        schema_version="1.0.0",
        group_id="g0",
        candidate_set_fingerprint="sha256:" + "a" * 64,
        tables=(),
        columns=(),
        relations=(),
        review_required=True,
    ).canonical_json()


async def test_fake_and_disabled_use_shared_semantic_provider_contract() -> None:
    request = contract_request()
    await assert_llm_provider_contract(
        LLMProviderContractCase(
            provider=fake_provider(output()), request=request, expected_output=output()
        )
    )
    await assert_llm_provider_contract(
        LLMProviderContractCase(
            provider=NoLLMProvider(),
            request=request,
            expected_error=LLMErrorCode.POLICY_DENIED,
        )
    )


@pytest.mark.parametrize("native", [False, True])
async def test_http_modes_keep_schema_prompt_and_no_tools(native: bool) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "scripted",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": output()},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=fake_provider().capabilities.model_copy(
                update={
                    "json_schema": native,
                    "max_input_tokens": 32768,
                    "context_window_tokens": 40000,
                }
            ),
        ),
        prompts=(semantic_mapping_prompt(),),
        schemas=(semantic_mapping_response_schema(),),
        transport=httpx.MockTransport(handle),
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    )
    async with provider:
        await assert_llm_provider_contract(
            LLMProviderContractCase(
                provider=provider, request=contract_request(), expected_output=output()
            )
        )
    assert len(seen) == 1
    wire = json.loads(seen[0].content)
    assert wire["response_format"]["type"] == (
        "json_schema" if native else "json_object"
    )
    assert "tools" not in wire and "functions" not in wire
    assert wire["messages"][0]["content"] == semantic_mapping_prompt().text
    assert [m["content"] for m in wire["messages"] if m["role"] == "user"] == [
        contract_request().payload_json
    ]
