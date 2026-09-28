"""Trusted projector не обходит approval, budgets или независимую validation."""

import json
import traceback
from collections.abc import Callable
from typing import cast

import httpx
import pytest
from tests.fakes.llm import fixed_clock, request_for
from tests.unit.llm.test_http_provider import Result, capabilities, reply

from structuraguard.contracts._base import CanonicalValue, canonical_sha256_value
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import (
    LLMPromptTemplate,
    LLMResponseSchema,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)

type Projector = Callable[[dict[str, object], str], dict[str, object]]


def provider_for_projection(
    projector: Projector,
    transport: httpx.AsyncBaseTransport,
    *,
    native_schema: bool = True,
) -> tuple[LLMResponseSchema, OpenAICompatibleProvider]:
    schema = LLMResponseSchema(
        schema_id="test-result",
        version="1.0.0",
        model=Result,
        decoding_projector=projector,
    )
    return schema, OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=capabilities(native_schema=native_schema),
        ),
        prompts=(
            LLMPromptTemplate(
                prompt_id="semantic_parse_plan", version="1.0.0", text="prompt v1"
            ),
        ),
        schemas=(schema,),
        transport=transport,
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    )


def value_schema(schema: dict[str, object]) -> dict[str, object]:
    properties = schema["properties"]
    assert isinstance(properties, dict)
    value = properties["value"]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


@pytest.mark.anyio
async def test_projection_uses_owned_copy_and_records_effective_fingerprint() -> None:
    schemas: list[dict[str, object]] = []

    def project(base: dict[str, object], payload: str) -> dict[str, object]:
        assert "const" not in value_schema(base)
        value_schema(base)["const"] = json.loads(payload)["sample"]
        return base

    def handle(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        projected = wire["response_format"]["json_schema"]["schema"]
        schemas.append(projected)
        return reply(json.dumps({"value": value_schema(projected)["const"]}))

    schema, provider = provider_for_projection(project, httpx.MockTransport(handle))
    original_json, original_fingerprint = schema.schema_json, schema.fingerprint
    responses = []
    async with provider:
        for value in ("first", "second"):
            responses.append(
                await provider.generate_structured(
                    request_for(json.dumps({"sample": value}, separators=(",", ":")))
                )
            )
    assert schema.schema_json == original_json
    assert schema.fingerprint == original_fingerprint
    assert len(schemas) == 2
    assert [response.decoding_schema_fingerprint for response in responses] == [
        canonical_sha256_value(cast(CanonicalValue, projected)) for projected in schemas
    ]
    assert (
        responses[0].decoding_schema_fingerprint
        != responses[1].decoding_schema_fingerprint
    )


@pytest.mark.anyio
async def test_json_object_fallback_never_projects_source_into_system_schema() -> None:
    def project(_base: dict[str, object], _payload: str) -> dict[str, object]:
        pytest.fail("JSON-object fallback должен сохранить статическую schema")

    def handle(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)
        system_schema = json.loads(
            wire["messages"][0]["content"].split("Response schema: ", 1)[1]
        )
        assert system_schema == json.loads(schema.schema_json)
        assert "private-source-key" not in wire["messages"][0]["content"]
        assert "private-source-key" in wire["messages"][-1]["content"]
        return reply()

    schema, provider = provider_for_projection(
        project, httpx.MockTransport(handle), native_schema=False
    )
    async with provider:
        result = await provider.generate_structured(
            request_for('{"sample":"private-source-key"}')
        )
    assert result.decoding_schema_fingerprint == schema.fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize("widen", [False, True])
async def test_decoder_constraints_and_original_dto_are_both_authoritative(
    widen: bool,
) -> None:
    def project(base: dict[str, object], _payload: str) -> dict[str, object]:
        value = value_schema(base)
        if widen:
            value["type"] = "integer"
        else:
            value["const"] = "expected"
        return base

    content = '{"value":1}' if widen else '{"value":"unexpected"}'
    _, provider = provider_for_projection(
        project, httpx.MockTransport(lambda _: reply(content))
    )
    async with provider:
        with pytest.raises(LLMProviderError, match="LLM_SCHEMA_VIOLATION"):
            await provider.generate_structured(request_for())


@pytest.mark.anyio
@pytest.mark.parametrize("padding", [10000, 70000])
async def test_projection_is_counted_in_wire_and_schema_budgets(padding: int) -> None:
    def project(base: dict[str, object], _payload: str) -> dict[str, object]:
        base["description"] = "x" * padding
        return base

    def no_http(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Превышение budget должно остановиться до HTTP")

    _, provider = provider_for_projection(project, httpx.MockTransport(no_http))
    async with provider:
        with pytest.raises(LLMProviderError, match="LLM_CONTEXT_LIMIT"):
            await provider.generate_structured(request_for())
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_private_projector_exception_is_sanitized() -> None:
    def project(_base: dict[str, object], _payload: str) -> dict[str, object]:
        raise RuntimeError("private-projector-canary")

    _, provider = provider_for_projection(
        project, httpx.MockTransport(lambda _: reply())
    )
    async with provider:
        with pytest.raises(LLMProviderError) as caught:
            await provider.generate_structured(request_for())
    assert caught.value.error_code == "LLM_REQUEST_INVALID"
    visible = (
        str(caught.value)
        + repr(caught.value.details)
        + "".join(traceback.format_exception(caught.value))
        + repr(provider.calls)
    )
    assert "private-projector-canary" not in visible
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_payload_approval_precedes_projector() -> None:
    def project(_base: dict[str, object], _payload: str) -> dict[str, object]:
        pytest.fail("Недействительный approval не должен достигать projector")

    _, provider = provider_for_projection(
        project, httpx.MockTransport(lambda _: reply())
    )
    request = request_for().model_copy(update={"payload_json": '{"sample":"changed"}'})
    async with provider:
        with pytest.raises(LLMProviderError, match="LLM_REQUEST_INVALID"):
            await provider.generate_structured(request)
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_projected_external_reference_is_rejected_before_http() -> None:
    def project(base: dict[str, object], _payload: str) -> dict[str, object]:
        value_schema(base)["$ref"] = "https://untrusted.invalid/schema"
        return base

    _, provider = provider_for_projection(
        project, httpx.MockTransport(lambda _: reply())
    )
    async with provider:
        with pytest.raises(LLMProviderError, match="LLM_REQUEST_INVALID"):
            await provider.generate_structured(request_for())
    assert provider.call_count == 0
