"""Line-only log catalog сохраняет все события в ограниченном model context."""

import json
from decimal import Decimal

import httpx
import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import (
    PipelineStatus,
    ProviderCapabilities,
    SemanticParsingMode,
)
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.llm import LLMExecutionEnvironment
from structuraguard.contracts.semantic import (
    LLMStructurePolicy,
    LLMStructureSuggestion,
    SemanticEntityProposal,
    SemanticFieldProposal,
    SemanticPathStep,
    SemanticPlanProposal,
    SemanticSelector,
)
from structuraguard.llm import OpenAICompatibleConfig, OpenAICompatibleProvider
from structuraguard.parsers.builtin import LogParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.structure import semantic_prompt, semantic_response_schema


@pytest.mark.anyio
async def test_syslog_request_preserves_nineteen_events_in_native_context() -> None:
    rows = [
        {
            "requestID": f"00000000-0000-4000-8000-{index:012}",
            "date": "2026-01-01",
            "time": f"10:00:{index:02}",
            "instanceID": "00000000-0000-4000-8000-000000000101",
            "level": "INFO",
            "message": "Job started" if index % 2 else "No jobs available",
            "context": {"attempt": 1},
        }
        for index in range(19)
    ]
    lines = [
        f"2026-01-01T10:00:{index:02}Z node-alpha worker[101]: "
        + json.dumps(row, separators=(",", ":"))
        for index, row in enumerate(rows)
    ]
    content = ("\n".join(lines) + "\n").encode()
    assert 4500 < len(content) < 6000
    source = source_for(content, display_name="events.log")
    batches = await collect(LogParser(), source, contexts_for(source, content)[1])
    seen: list[httpx.Request] = []
    paths: list[tuple[str, ...]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        wire = json.loads(request.content)
        payload = json.loads(wire["messages"][-1]["content"])
        scope = tuple(item["ref"] for item in payload["source_catalog"])
        paths.extend(
            tuple(path) for path in payload["log_json_shapes"][0]["scalar_paths"]
        )
        fields = tuple(
            SemanticFieldProposal(
                field_id=f"f{index}",
                semantic_name=path[-1].lower(),
                semantic_type="string",
                locale_hint=None,
                source_refs=scope[:4],
                selector=SemanticSelector(
                    kind="log_json",
                    index=None,
                    offset=0,
                    path=tuple(
                        SemanticPathStep(operation="key", name=key, occurrence=0)
                        for key in path
                    ),
                    value_source=None,
                    delimiter=None,
                    target=None,
                    key_equals=None,
                ),
            )
            for index, path in enumerate(paths)
        )
        suggestion = LLMStructureSuggestion(
            schema_version="1.0.0",
            decision="plan",
            candidate_ids=("c0",),
            self_confidence=0.99,
            plan=SemanticPlanProposal(
                kind="log",
                root_ref=None,
                header_row=None,
                data_start_row=None,
                data_end_row=None,
                footer_start_row=None,
                repeated_header_rows=(),
                scope=scope,
                fields=fields,
                entities=(
                    SemanticEntityProposal(
                        entity_id="records",
                        entity_type="events",
                        parent_entity_id=None,
                        field_ids=tuple(f"f{index}" for index in range(len(paths))),
                        path=(),
                        records=tuple((ref,) for ref in scope),
                    ),
                ),
            ),
        )
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": suggestion.canonical_json(),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 6000, "completion_tokens": 1000},
            },
        )

    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            endpoint="http://127.0.0.1:9999/v1/chat/completions",
            capabilities=ProviderCapabilities(
                provider_id="test-http",
                provider_version="1.0.0",
                model_id="test-model",
                structured_output=True,
                json_schema=True,
                supported_purposes=("semantic_parsing",),
                execution_environment=LLMExecutionEnvironment.LOCAL,
                max_input_bytes=65536,
                max_output_bytes=65536,
                max_input_tokens=24576 - 3072,
                max_output_tokens=3072,
                context_window_tokens=24576,
            ),
        ),
        prompts=(semantic_prompt(),),
        schemas=(semantic_response_schema(),),
        transport=httpx.MockTransport(handle),
        clock=fixed_clock,
        monotonic=lambda: 0,
    )
    async with (
        provider,
        SemanticParsingSession(
            replay=lambda: stream(batches),
            provider=provider,
            scanner=Scanner(),
            context=context(),
            clock=fixed_clock,
            timer=lambda: 0,
            policy=ParsingPolicy(
                mode=SemanticParsingMode.LLM_FIRST,
                structural=LLMStructurePolicy(
                    max_sample_values=24, confidence_threshold=Decimal("0.9")
                ),
            ),
        ) as session,
    ):
        output = [batch async for batch in session.parse_semantically()]
        assert len(seen) == 1
        assert len(seen[0].content) + 1024 <= 24576 - 3072
        wire = json.loads(seen[0].content)
        assert "compact SINGLE-LINE JSON" in wire["messages"][0]["content"]
        assert "complete source scope" in wire["messages"][0]["content"]
        assert "1-4 representative LINE aliases" in wire["messages"][0]["content"]
        assert "parsing_policy.locales or null" in wire["messages"][0]["content"]
        assert 'decision="plan" and a non-null plan' in wire["messages"][0]["content"]
        payload = json.loads(wire["messages"][-1]["content"])
        assert all(item["kind"] == "line" for item in payload["source_catalog"])
        assert len(payload["source_catalog"]) == len(payload["samples"]) == 19
        assert [item["raw"]["value"] for item in payload["samples"]] == lines
        assert all(item["evidence"] for item in payload["profile"]["observations"])
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED, session.report.issues
        assert len(seen) == session.report.llm_calls == 1
        assert len(paths) == 7
        assert session.report.plan is not None
        assert session.report.plan.semantic_analysis is not None
        assert (
            session.report.plan.semantic_analysis.response_schema_fingerprint
            == canonical_sha256_value(wire["response_format"]["json_schema"]["schema"])
        )
        assert [
            [value.normalized_value.value for value in record.entities[0].values]
            for batch in output
            for record in batch.records
        ] == [
            ["1" if len(path) == 2 else row[path[0]] for path in paths] for row in rows
        ]
        assert all(
            value.origins[0].source_ref.kind.value == "line"
            for batch in output
            for record in batch.records
            for value in record.entities[0].values
        )
