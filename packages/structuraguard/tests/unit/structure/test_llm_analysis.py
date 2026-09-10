"""LLM semantic analysis: настоящий parser/validator и offline scripted provider."""

from functools import partial
from typing import Literal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, Validator, context, encoded, scenario
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import SemanticParsingMode
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.parsing import (
    StructureNeedsReview,
    StructurePlanCreated,
    StructureRejected,
    TabularParsePlan,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import FakeLLMProvider, ScriptedFailure, ScriptedResponse
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    LogParser,
    MarkdownParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.ports.semantic import SemanticStructureAnalyzer
from structuraguard.structure import LLMStructureAnalyzer, semantic_response_schema


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content",
    [
        (
            DelimitedTextParser(),
            b"Report\nname,amount\nAda,12\nBob,20\nname,amount\nCara,4\nTotal,36\n",
        ),
        (
            JsonDocumentParser(),
            b'[{"id":1,"items":[{"sku":"a"},{"sku":"b"}]},{"id":2,"items":[{"sku":"c"}]}]',
        ),
        (
            LogParser(),
            b"2026-09-10T12:00:00Z INFO user=1 started\n  detail one\n2026-09-10T12:01:00Z ERROR user=2 failed\n  detail two\n",
        ),
        (MarkdownParser(), b"Name: Ada\n\nName: Bob\n"),
    ],
    ids=["regions", "parent-child-paths", "log-boundaries", "document-targets"],
)
async def test_valid_plan_requires_physical_validation_and_keeps_provenance(
    parser: Parser, content: bytes
) -> None:
    request, batches, output = await scenario(parser, content)
    scanner, validator = Scanner(), Validator()
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    analyzer = LLMStructureAnalyzer(
        provider=provider, scanner=scanner, validator=validator, context=context()
    )
    assert isinstance(analyzer, SemanticStructureAnalyzer)
    result = await analyzer.analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructurePlanCreated)
    assert validator.calls == 1 and provider.call_count == 1
    assert result.plan.confidence != output["self_confidence"]
    assert all(
        field.semantic_type == "string" and field.locale_hint == "ru-RU"
        for field in result.plan.fields
    )
    assert result.plan.semantic_analysis is not None
    assert (
        result.plan.semantic_analysis.generation_fingerprint
        == provider.calls[0].generation_fingerprint
    )
    assert result.plan.semantic_analysis.prompt.version == "1.0.0"
    assert "UNTRUSTED_SOURCE_DATA" in scanner.requests[0].payload_json
    assert (
        len(scanner.requests[0].payload_json.encode())
        <= analyzer.policy.max_payload_bytes
    )
    assert request.source.artifact_id not in scanner.requests[0].payload_json
    if isinstance(result.plan, TabularParsePlan):
        assert result.plan.header_row == 1 and result.plan.footer_start_row == 6
        assert result.plan.repeated_header_rows == (4,)


@pytest.mark.anyio
@pytest.mark.parametrize("code", [LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT])
async def test_provider_failure_is_typed_without_retry(
    code: Literal[LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT],
) -> None:
    request, batches, _ = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    provider = FakeLLMProvider((ScriptedFailure(code=code),), clock=fixed_clock)
    validator = Validator()
    analyzer = LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=validator, context=context()
    )
    with pytest.raises(LLMProviderError) as error:
        await analyzer.analyze(request, replay=partial(stream, batches))
    assert error.value.error_code == code.value
    assert provider.call_count == 1 and validator.calls == 0


@pytest.mark.anyio
async def test_ambiguity_is_needs_review_even_with_maximum_self_confidence() -> None:
    request, batches, _ = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    output = dict(
        schema_version="1.0.0",
        decision="ambiguous",
        candidate_ids=["c0"],
        self_confidence=1.0,
        plan=None,
    )
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    validator = Validator()
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=validator, context=context()
    ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructureNeedsReview)
    assert result.issues[0].code == "LLM_AMBIGUOUS"
    assert validator.calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("raw", ["{", "{}", '{"schema_version":"1.0.0","extra":true}'])
async def test_malformed_suggestion_cannot_reach_validator(raw: str) -> None:
    request, batches, _ = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    provider = FakeLLMProvider((ScriptedResponse(output_json=raw),), clock=fixed_clock)
    validator = Validator()
    with pytest.raises(LLMProviderError):
        await LLMStructureAnalyzer(
            provider=provider, scanner=Scanner(), validator=validator, context=context()
        ).analyze(request, replay=partial(stream, batches))
    assert validator.calls == 0


@pytest.mark.anyio
async def test_deterministic_or_missing_replay_never_calls_llm() -> None:
    request, batches, _ = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    provider = FakeLLMProvider((), clock=fixed_clock)
    analyzer = LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=Validator(), context=context()
    )
    assert isinstance(await analyzer.analyze(request), StructureRejected)
    assert isinstance(
        await analyzer.analyze(
            request.model_copy(update={"mode": SemanticParsingMode.DETERMINISTIC}),
            replay=partial(stream, batches),
        ),
        StructureRejected,
    )
    assert provider.call_count == 0


def test_generation_schema_is_closed_and_bounded() -> None:
    schema = semantic_response_schema()
    assert '"additionalProperties":false' in schema.schema_json
    assert len(schema.schema_json.encode()) < 65536


@pytest.mark.anyio
async def test_tenfold_row_growth_keeps_one_call_and_a_bounded_sample() -> None:
    import json

    for count in (10, 100):
        request, batches, output = await scenario(
            DelimitedTextParser(), b"name,n\n" + b"Ada,1\n" * count
        )
        scanner, validator = Scanner(), Validator()
        provider = FakeLLMProvider(
            (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
        )
        result = await LLMStructureAnalyzer(
            provider=provider, scanner=scanner, validator=validator, context=context()
        ).analyze(request, replay=partial(stream, batches))
        assert isinstance(result, StructurePlanCreated)
        assert isinstance(result.plan, TabularParsePlan)
        assert result.plan.data_end_row == count
        assert provider.call_count == validator.calls == 1
        payload = scanner.requests[0].payload_json
        assert len(payload.encode()) <= 65536
        assert len(json.loads(payload)["samples"]) <= 100


@pytest.mark.anyio
async def test_two_log_variants_preserve_disjoint_records() -> None:
    from copy import deepcopy
    from typing import cast

    from structuraguard.contracts.parsing import LogParsePlan

    request, batches, output = await scenario(
        LogParser(),
        b"2026-09-10T12:00:00Z INFO user=1 started\n  detail one\n2026-09-10T12:01:00Z ERROR user=2 failed\n  detail two\n",
    )
    plan = cast(dict[str, object], output["plan"])
    fields = cast(list[dict[str, object]], plan["fields"])
    original = cast(list[dict[str, object]], plan["entities"])[0]
    records = cast(list[list[str]], original["records"])
    variants, all_fields = [], []
    for index, record in enumerate(records):
        current = deepcopy(fields)
        for field in current:
            field["field_id"] = f"v{index}_{field['field_id']}"
            field["semantic_name"] = f"v{index}_{field['semantic_name']}"
            field["source_refs"] = [record[0]]
        variants.append(
            {
                **original,
                "entity_id": f"variant_{index}",
                "entity_type": f"event_{index}",
                "field_ids": [f["field_id"] for f in current],
                "records": [record],
            }
        )
        all_fields.extend(current)
    plan.update(fields=all_fields, entities=variants)
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=Validator(), context=context()
    ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructurePlanCreated)
    assert isinstance(result.plan, LogParsePlan)
    assert len(result.plan.entities) == 2
    assert result.plan.entities[0].grouping != result.plan.entities[1].grouping


@pytest.mark.anyio
async def test_partial_row_scope_requires_review_after_validation() -> None:
    from typing import cast

    request, batches, output = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    cast(dict[str, object], output["plan"])["data_end_row"] = 1
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    validator = Validator()
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=validator, context=context()
    ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructureNeedsReview)
    assert result.issues[0].code == "LLM_INCOMPLETE_SCOPE"
    assert validator.calls == 1


@pytest.mark.anyio
async def test_deterministic_low_score_overrides_maximum_model_confidence() -> None:
    from structuraguard.contracts.parsing import StructureAnalysisRequest

    request, batches, output = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    profile = request.profile.model_dump()
    profile["candidates"] = [
        {**c.model_dump(exclude={"assessment"}), "confidence": "0.4"}
        for c in request.profile.candidates
    ]
    profile["profile_fingerprint"] = "sha256:" + "0" * 64
    request = StructureAnalysisRequest.model_validate(
        {**request.model_dump(), "profile": profile}
    )
    output["self_confidence"] = 1.0
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    validator = Validator()
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=validator, context=context()
    ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructureNeedsReview)
    assert result.issues[0].code == "STRUCTURE_CONFIDENCE_LOW"
    assert validator.calls == 1


@pytest.mark.anyio
async def test_identical_inputs_and_controlled_clocks_produce_identical_lineage() -> (
    None
):
    results = []
    for _ in range(2):
        request, batches, output = await scenario(
            DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
        )
        provider = FakeLLMProvider(
            (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
        )
        results.append(
            await LLMStructureAnalyzer(
                provider=provider,
                scanner=Scanner(),
                validator=Validator(),
                context=context(),
            ).analyze(request, replay=partial(stream, batches))
        )
    assert results[0].canonical_json() == results[1].canonical_json()


@pytest.mark.anyio
@pytest.mark.parametrize("native_schema", [True, False])
async def test_http_adapter_and_analyzer_share_versioned_schema_offline(
    native_schema: bool,
) -> None:
    import json

    import httpx
    from tests.unit.llm.test_http_provider import capabilities, reply

    from structuraguard.contracts.semantic import LLMStructurePolicy
    from structuraguard.llm import OpenAICompatibleConfig, OpenAICompatibleProvider
    from structuraguard.structure import semantic_prompt

    request, batches, output = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    wire: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        return reply(encoded(output))

    async with OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=capabilities(native_schema=native_schema).model_copy(
                update={"max_input_tokens": 32768, "context_window_tokens": 65536}
            ),
        ),
        prompts=(semantic_prompt(),),
        schemas=(semantic_response_schema(),),
        transport=httpx.MockTransport(handle),
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    ) as provider:
        result = await LLMStructureAnalyzer(
            provider=provider,
            scanner=Scanner(),
            validator=Validator(),
            context=context(),
            policy=LLMStructurePolicy(max_response_bytes=4096),
        ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructurePlanCreated)
    assert len(wire) == 1 and "tools" not in wire[0]
    assert result.plan.semantic_analysis is not None
    assert result.plan.semantic_analysis.provider_id == "test-http"


@pytest.mark.anyio
async def test_model_cannot_hide_another_deterministic_candidate() -> None:
    from structuraguard.contracts.parsing import StructureAnalysisRequest

    request, batches, output = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    profile = request.profile.model_dump()
    candidate = request.profile.candidates[0].model_dump()
    profile["candidates"] = [candidate, {**candidate, "candidate_id": "alternative"}]
    profile["profile_fingerprint"] = "sha256:" + "0" * 64
    request = StructureAnalysisRequest.model_validate(
        {**request.model_dump(), "profile": profile}
    )
    output["candidate_ids"] = ["c0"]
    output["self_confidence"] = 1.0
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock
    )
    validator = Validator()
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=validator, context=context()
    ).analyze(request, replay=partial(stream, batches))
    assert isinstance(result, StructureNeedsReview)
    assert result.issues[0].code == "LLM_AMBIGUOUS"
    assert validator.calls == 1
