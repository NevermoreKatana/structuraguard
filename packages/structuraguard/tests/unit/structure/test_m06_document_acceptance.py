"""K2/K6: смена adapter, physical occurrence identity и конфликтующие связи."""

import json
from functools import partial

import httpx
import pytest
from tests.fakes.documents import chunks_for, document_script, physical, suggestion
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.llm.test_http_provider import capabilities, reply
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus
from structuraguard.contracts.semantic import LLMStructurePolicy
from structuraguard.llm import (
    FakeLLMProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    ScriptedResponse,
)
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.structure import document_prompt, document_response_schema

pytestmark = pytest.mark.anyio


async def test_equal_values_at_distinct_refs_survive_overlap_and_batch_changes() -> (
    None
):
    projections = []
    for batch_size in (1, 100):
        batches = await physical(
            MarkdownParser(),
            b"Ada agrees.\n\nAda agrees.\n\nAda agrees.\n",
            batch_size=batch_size,
        )
        policy = ParsingPolicy(chunk_fragments=2, overlap_fragments=1)
        provider = FakeLLMProvider(
            await document_script(batches, policy), clock=fixed_clock
        )
        async with SemanticParsingSession(
            replay=partial(stream, batches),
            policy=policy,
            provider=provider,
            scanner=Scanner(),
            context=context(),
            clock=fixed_clock,
            timer=lambda: 0,
        ) as session:
            output = [b async for b in session.parse_semantically()]
            assert (
                session.report is not None
                and session.report.status is PipelineStatus.COMPLETED
            )
            values = [
                v
                for b in output
                for r in b.records
                for e in r.entities
                for v in e.values
            ]
            assert len(values) == 3 and provider.call_count == 2
            assert len({v.source_refs for v in values}) == 3
            projections.append(
                [(v.normalized_value.value, v.origins[0].location) for v in values]
            )
    assert projections[0] == projections[1]


async def test_multispan_value_is_reconstructed_from_both_physical_sources() -> None:
    from structuraguard.structure.text_sources import text_sources

    batches = await physical(MarkdownParser(), b"Ada agrees.\n\nBob accepts.\n")
    proposed = suggestion((await chunks_for(batches, ParsingPolicy()))[0])
    data = proposed.model_dump()
    left, right = data["entities"]
    left["fields"][0]["spans"] += right["fields"][0]["spans"]
    data["entities"] = (left,)
    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json=type(proposed).model_validate(data).canonical_json()
            ),
        ),
        clock=fixed_clock,
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        output = [b async for b in session.parse_semantically()]
        assert (
            session.report is not None
            and session.report.status is PipelineStatus.COMPLETED
        )
        values = [
            v for b in output for r in b.records for e in r.entities for v in e.values
        ]
        assert len(values) == 1
        value = values[0]
        assert value.normalized_value.value == "Ada Bob"
        original = {s.ref: s.text for b in batches for s in text_sources(b)}
        assert len(value.source_refs) == 2
        assert (
            " ".join(
                original[span.source_ref][span.start : span.end]
                for origin in value.origins
                for span in origin.source_spans
            )
            == value.normalized_value.value
        )


@pytest.mark.parametrize("conflict", ("field", "parent", "cycle", "missing_parent"))
async def test_conflicting_fields_and_parents_never_produce_orphans(
    conflict: str,
) -> None:
    batches = await physical(
        MarkdownParser(), b"Ada agrees.\n\nBob accepts.\n\nFee 1200.\n"
    )
    policy = ParsingPolicy(chunk_fragments=2, overlap_fragments=1)
    chunks = await chunks_for(batches, policy)
    first, second = suggestion(chunks[0]), suggestion(chunks[1])
    a, b = first.model_dump(), second.model_dump()
    if conflict == "field":
        b["entities"][0]["fields"][0]["spans"] = (b["entities"][0]["anchor"],)
    elif conflict == "parent":
        a["entities"][1]["parent_entity_id"] = "e0"
    elif conflict == "cycle":
        a["entities"][0]["parent_entity_id"] = "e1"
        a["entities"][1]["parent_entity_id"] = "e0"
    else:
        a["entities"][1]["parent_entity_id"] = "unknown"
    provider = FakeLLMProvider(
        tuple(
            ScriptedResponse(
                output_json=type(first).model_validate(data).canonical_json()
            )
            for data in (a, b)
        ),
        clock=fixed_clock,
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=policy,
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        output = [b async for b in session.parse_semantically()]
        report = session.report
        assert report is not None and report.status is PipelineStatus.NEEDS_REVIEW
        assert report.normalized_fingerprint is None and report.unresolved_refs
        assert not any(batch.is_last for batch in output)
        for batch in output:
            for record in batch.records:
                ids = {e.entity_id for e in record.entities}
                assert all(
                    e.parent_entity_id is None or e.parent_entity_id in ids
                    for e in record.entities
                )
        expected = (
            "LLM_UNKNOWN_SOURCE_REFERENCE"
            if conflict == "missing_parent"
            else "SEMANTIC_ENTITY_CONFLICT"
        )
        assert expected in {issue.code for issue in report.issues}
        assert sum(len(batch.records) for batch in output) == (
            1 if conflict == "cycle" else 2
        )


@pytest.mark.parametrize("native_schema", (True, False))
async def test_document_provider_swap_preserves_validation_and_source_values(
    native_schema: bool,
) -> None:
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    policy = ParsingPolicy(structural=LLMStructurePolicy(max_response_bytes=4096))
    script = await document_script(batches, policy)
    wire: list[dict[str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        wire.append(json.loads(request.content))
        return reply(script[0].output_json)

    projections = []
    async with OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=capabilities(native_schema=native_schema).model_copy(
                update={"max_input_tokens": 32768, "context_window_tokens": 65536}
            ),
        ),
        prompts=(document_prompt(),),
        schemas=(document_response_schema(),),
        transport=httpx.MockTransport(handle),
        clock=fixed_clock,
        monotonic=lambda: 0,
    ) as http_provider:
        for provider in (FakeLLMProvider(script, clock=fixed_clock), http_provider):
            async with SemanticParsingSession(
                replay=lambda: stream(batches),
                policy=policy,
                provider=provider,
                scanner=Scanner(),
                context=context(),
                clock=fixed_clock,
                timer=lambda: 0,
            ) as session:
                output = [b async for b in session.parse_semantically()]
                report = session.report
                assert report is not None and report.status is PipelineStatus.COMPLETED
                assert report.provenance_coverage == report.llm_calls == 1
                assert report.plan is not None and report.plan.semantic_generations
                assert (
                    report.provider_metadata[0].provider_id
                    == provider.capabilities.provider_id
                )
                projections.append(
                    [
                        (v.normalized_value, v.source_refs, v.origins)
                        for b in output
                        for r in b.records
                        for e in r.entities
                        for v in e.values
                    ]
                )
    assert projections[0] == projections[1]
    assert len(wire) == 1 and "tools" not in wire[0]
    response_format = wire[0]["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == (
        "json_schema" if native_schema else "json_object"
    )
