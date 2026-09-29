"""Смешанный syslog: несколько JSON shapes и HTTP-текст сохраняют все события."""

import json
from decimal import Decimal

import httpx
import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context, encoded
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import (
    PipelineStatus,
    ProviderCapabilities,
    SemanticParsingMode,
)
from structuraguard.contracts.llm import LLMExecutionEnvironment
from structuraguard.contracts.source import LineRangeLocation
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import OpenAICompatibleConfig, OpenAICompatibleProvider
from structuraguard.parsers.builtin import LogParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.structure import semantic_prompt, semantic_response_schema

MESSAGES = (
    '{"level":"INFO","message":"Started"}',
    '10.0.0.1 - - "GET /health HTTP/1.1" 200 7',
    '{"message":"Done","status":null}',
    '10.0.0.2 - - "POST /jobs HTTP/1.1" 202 8',
    '{"level":"INFO","message":"Stopped"}',
)
LINES = tuple(
    f"2026-01-01T10:00:0{i}Z node worker[101]: {message}"
    for i, message in enumerate(MESSAGES)
)


def mixed_payload() -> dict[str, object]:
    return {
        "source_catalog": [{"ref": f"r{i}", "kind": "line"} for i in range(5)],
        "parsing_policy": {"max_fields": 64, "max_entities": 16},
        "log_json_shapes": [
            {
                "refs": ["r0", "r4"],
                "selector": "log_json",
                "offset": 0,
                "scalar_paths": [["level"], ["message"]],
            },
            {
                "refs": ["r2"],
                "selector": "log_json",
                "offset": 0,
                "scalar_paths": [["message"], ["status"]],
            },
        ],
        "log_raw_records": ["r1", "r3"],
    }


def mixed_suggestion() -> dict[str, object]:
    fields = []
    for index, (name, key, refs) in enumerate(
        (
            ("level", "level", ["r0", "r4"]),
            ("message", "message", ["r0", "r4"]),
            ("result_message", "message", ["r2"]),
            ("status", "status", ["r2"]),
            ("http_event", None, ["r1", "r3"]),
        )
    ):
        fields.append(
            {
                "field_id": f"f{index}",
                "semantic_name": name,
                "semantic_type": "string",
                "locale_hint": None,
                "source_refs": refs,
                "selector": {
                    "kind": "log_json" if key else "log_record",
                    "index": None,
                    "offset": 0 if key else None,
                    "path": [{"operation": "key", "name": key, "occurrence": 0}]
                    if key
                    else [],
                    "value_source": None,
                    "delimiter": None,
                    "target": None,
                    "key_equals": None,
                },
            }
        )
    return {
        "schema_version": "1.0.0",
        "decision": "plan",
        "candidate_ids": [],
        "self_confidence": 0.99,
        "plan": {
            "kind": "log",
            "root_ref": None,
            "header_row": None,
            "data_start_row": None,
            "data_end_row": None,
            "footer_start_row": None,
            "repeated_header_rows": [],
            "scope": [f"r{i}" for i in range(5)],
            "fields": fields,
            "entities": [
                {
                    "entity_id": f"records_{i}",
                    "entity_type": name,
                    "field_ids": ids,
                    "parent_entity_id": None,
                    "path": [],
                    "records": [[ref] for ref in refs],
                }
                for i, (name, ids, refs) in enumerate(
                    (
                        ("application", ["f0", "f1"], ["r0", "r4"]),
                        ("result", ["f2", "f3"], ["r2"]),
                        ("http_access", ["f4"], ["r1", "r3"]),
                    )
                )
            ],
        },
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "invented_path",
        "text_as_json",
        "dropped",
        "duplicate",
        "reordered",
        "wrong_type",
    ],
)
def test_mixed_native_schema_rejects_unproven_plan(mutation: str) -> None:
    prepared = semantic_response_schema().prepare_decoding(json.dumps(mixed_payload()))
    value = json.loads(encoded(mixed_suggestion()))
    prepared.validate(json.dumps(value))
    plan = value["plan"]
    if mutation == "invented_path":
        plan["fields"][0]["selector"]["path"][0]["name"] = "syslog_timestamp"
    elif mutation == "text_as_json":
        plan["entities"][0]["records"].append(["r1"])
        plan["entities"][2]["records"].remove(["r1"])
    elif mutation == "dropped":
        plan["entities"][2]["records"].pop()
    elif mutation == "duplicate":
        plan["entities"][2]["records"] = [["r1"], ["r1"]]
    elif mutation == "reordered":
        plan["entities"][0]["records"].reverse()
    else:
        plan["fields"][-1]["semantic_type"] = "integer"
    with pytest.raises(LLMProviderError, match="LLM_SCHEMA_VIOLATION"):
        prepared.validate(json.dumps(value))


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "overlap",
        "unknown",
        "reversed",
        "malformed",
        "field_budget",
        "entity_budget",
    ],
)
def test_incomplete_or_unbounded_partition_keeps_generic_schema(mutation: str) -> None:
    payload = json.loads(json.dumps(mixed_payload()))
    if mutation == "missing":
        payload["log_raw_records"].pop()
    elif mutation == "overlap":
        payload["log_raw_records"].append("r0")
    elif mutation == "unknown":
        payload["log_raw_records"].append("r999")
    elif mutation == "reversed":
        payload["log_raw_records"].reverse()
    elif mutation == "malformed":
        payload["log_raw_records"] = None
    else:
        payload["parsing_policy"][
            "max_fields" if mutation == "field_budget" else "max_entities"
        ] = 2
    base = semantic_response_schema()
    assert base.prepare_decoding(json.dumps(payload)).schema_json == base.schema_json


@pytest.mark.anyio
@pytest.mark.parametrize("batch_size", [1, 100])
async def test_native_mixed_plan_replays_all_events_without_confidence_penalty(
    batch_size: int,
) -> None:
    content = ("\n".join(LINES) + "\n").encode()
    source = source_for(content, display_name="mixed.log")
    batches = await collect(
        LogParser(), source, contexts_for(source, content, batch_size=batch_size)[1]
    )
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        payload = json.loads(body["messages"][-1]["content"])
        expected = mixed_payload()
        assert payload["log_json_shapes"] == expected["log_json_shapes"]
        assert payload["log_raw_records"] == expected["log_raw_records"]
        schema = body["response_format"]["json_schema"]["schema"]
        assert "$defs" not in schema  # Выбран именно доказанный mixed projection.
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": encoded(mixed_suggestion()),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 1000},
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
                max_input_bytes=131072,
                max_output_bytes=65536,
                max_input_tokens=100000,
                max_output_tokens=4096,
                context_window_tokens=110000,
            ),
        ),
        prompts=(semantic_prompt(),),
        schemas=(semantic_response_schema(),),
        transport=httpx.MockTransport(respond),
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
            policy=ParsingPolicy(mode=SemanticParsingMode.LLM_FIRST),
        ) as session,
    ):
        output = [batch async for batch in session.parse_semantically()]
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED, session.report.issues
        assert len(seen) == session.report.llm_calls == 1
        assert session.report.assessment is not None
        assert session.report.assessment.confidence >= Decimal("0.9")
        assert session.report.assessment.agreement == 1
        assert session.report.assessment.penalty == 0
        records = [record for batch in output for record in batch.records]
        assert len(records) == 5
        assert [
            [value.normalized_value.value for value in record.entities[0].values]
            for record in records
        ] == [
            ["INFO", "Started"],
            [LINES[1]],
            ["Done", None],
            [LINES[3]],
            ["INFO", "Stopped"],
        ]
        for index, record in enumerate(records):
            assert len(record.entities) == 1
            for value in record.entities[0].values:
                assert value.origins[0].raw_value.value == LINES[index]
                assert isinstance(value.origins[0].location, LineRangeLocation)
                assert value.origins[0].location.line_start == index + 1
