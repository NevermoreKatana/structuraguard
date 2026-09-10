"""Capabilities, prompt binding и совместимость serialized M2 DTO."""

import json

import pytest
from pydantic import ValidationError
from tests.fakes.llm import NOW, digest, fixed_clock, request_for

from structuraguard.contracts import LLMRequest, LLMResponse, ProviderCapabilities
from structuraguard.contracts.llm import LLMExecutionEnvironment, LLMPrompt
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import FakeLLMProvider, NoLLMProvider, ScriptedResponse


def test_legacy_capabilities_keep_wire_shape_and_unknown_locality() -> None:
    payload = {
        "provider_id": "test",
        "provider_version": "1.0.0",
        "model_id": "test-model",
        "structured_output": True,
        "supported_purposes": ["semantic_parsing"],
        "max_input_bytes": 4096,
        "max_output_bytes": 1024,
    }
    caps = ProviderCapabilities.model_validate(payload)
    assert caps.model_dump(mode="json") == payload
    assert caps.execution_environment is LLMExecutionEnvironment.UNKNOWN
    assert caps.context_window_tokens is None
    assert not caps.json_schema
    assert ProviderCapabilities.model_validate_json(caps.canonical_json()) == caps


@pytest.mark.parametrize(
    "change",
    [
        {"max_input_bytes": 0},
        {"max_output_bytes": 0},
        {"structured_output": False},
        {"max_input_tokens": True},
        {"max_output_tokens": -1},
        {"json_schema": "yes"},
        {"max_input_tokens": 32, "context_window_tokens": 31},
        {"execution_environment": "localhost"},
        {"supported_purposes": []},
    ],
)
def test_active_capabilities_reject_invalid_limits_and_flags(
    change: dict[str, object],
) -> None:
    caps = FakeLLMProvider((), clock=fixed_clock).capabilities
    with pytest.raises(ValidationError):
        ProviderCapabilities.model_validate({**caps.model_dump(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"structured_output": True},
        {"json_schema": True},
        {"tool_calling": True},
        {"max_input_bytes": 1},
        {"max_output_tokens": 1},
        {"supported_purposes": ["semantic_parsing"]},
    ],
)
def test_disabled_cannot_claim_generation(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProviderCapabilities.model_validate(
            {**NoLLMProvider().capabilities.model_dump(), **change}
        )


def test_legacy_request_and_response_do_not_grow_empty_prompt_metadata() -> None:
    request_payload = request_for().model_dump(mode="json")
    del request_payload["prompt"]
    request = LLMRequest.model_validate(request_payload)
    assert request.prompt is None
    assert request.model_dump(mode="json") == request_payload
    response = LLMResponse(
        request_id=request.request_id,
        provider_id="test",
        provider_version="1.0.0",
        model_id="test-model",
        response_schema_id=request.response_schema_id,
        response_schema_version=request.response_schema_version,
        output_json="{}",
        prompt_fingerprint=request.prompt_fingerprint,
        generation_fingerprint=digest("{}"),
        finish_reason="stop",
        input_tokens=0,
        output_tokens=0,
        generated_at=NOW,
    )
    assert "prompt" not in response.model_dump()
    assert LLMResponse.model_validate_json(response.canonical_json()) == response


@pytest.mark.anyio
async def test_both_dtos_reject_prompt_fingerprint_mismatch() -> None:
    request = request_for()
    response = await FakeLLMProvider(
        (ScriptedResponse(output_json="{}"),), clock=fixed_clock
    ).generate_structured(request)
    for dto in (request, response):
        with pytest.raises(ValidationError):
            type(dto).model_validate(
                {**dto.model_dump(), "prompt_fingerprint": digest("other")}
            )


@pytest.mark.anyio
@pytest.mark.parametrize("direction", ["input", "output"])
async def test_multibyte_byte_limit_accepts_n_rejects_n_plus_one(
    direction: str,
) -> None:
    body = json.dumps({"x": "Ё"}, ensure_ascii=False, separators=(",", ":"))
    request = request_for(body)
    for difference in (0, -1):
        base = FakeLLMProvider((), clock=fixed_clock).capabilities
        caps = ProviderCapabilities.model_validate(
            {
                **base.model_dump(),
                f"max_{direction}_bytes": len(body.encode()) + difference,
            }
        )
        provider = FakeLLMProvider(
            (ScriptedResponse(output_json=body),), clock=fixed_clock, capabilities=caps
        )
        if difference:
            with pytest.raises(
                LLMProviderError,
                match="LLM_CONTEXT_LIMIT"
                if direction == "input"
                else "LLM_INVALID_RESPONSE",
            ):
                await provider.generate_structured(request)
        else:
            assert (await provider.generate_structured(request)).output_json == body


@pytest.mark.anyio
async def test_response_respects_request_limit_and_script_token_usage() -> None:
    request = request_for().model_copy(update={"max_output_bytes": 2})
    provider = FakeLLMProvider(
        (
            ScriptedResponse(output_json='{"x":1}'),
            ScriptedResponse(output_json="{}", output_tokens=2049),
        ),
        clock=fixed_clock,
    )
    with pytest.raises(LLMProviderError, match="LLM_INVALID_RESPONSE"):
        await provider.generate_structured(request)
    with pytest.raises(LLMProviderError, match="LLM_CONTEXT_LIMIT"):
        await provider.generate_structured(request)


@pytest.mark.anyio
async def test_unsupported_purpose_or_missing_prompt_does_not_consume_step() -> None:
    provider = FakeLLMProvider((ScriptedResponse(output_json="{}"),), clock=fixed_clock)
    cases: list[tuple[dict[str, object], str]] = [
        ({"purpose": "semantic_mapping"}, "LLM_CAPABILITY_MISMATCH"),
        ({"prompt": None}, "LLM_REQUEST_INVALID"),
    ]
    for update, code in cases:
        request = request_for().model_copy(update=update)
        with pytest.raises(LLMProviderError, match=code):
            await provider.generate_structured(request)
    assert provider.call_count == 0


def test_prompt_is_versioned_frozen_and_extra_fields_are_forbidden() -> None:
    prompt = LLMPrompt(
        prompt_id="semantic.v1", version="1.0.0", fingerprint=digest("prompt")
    )
    with pytest.raises(ValidationError):
        prompt.version = "2.0.0"
    with pytest.raises(ValidationError):
        LLMPrompt.model_validate({**prompt.model_dump(), "headers": {}})
