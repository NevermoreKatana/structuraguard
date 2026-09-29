"""Подсказки JSON paths берутся только из подтверждённых samples без копий values."""

import json

import pytest
from tests.fakes.semantic import samples_for
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import stream

from structuraguard.contracts._base import canonical_json_value
from structuraguard.contracts.common import PhysicalObjectKind, SemanticParsingMode
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.parsing import StructureAnalysisRequest
from structuraguard.contracts.semantic import LLMStructurePolicy
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import LLMProviderError
from structuraguard.parsers.builtin import LogParser
from structuraguard.structure import DeterministicStructureAnalyzer
from structuraguard.structure.semantic_samples import prepare_samples


async def request_for(
    *payloads: str,
) -> tuple[StructureAnalysisRequest, tuple[ExtractedBatch, ...]]:
    content = "".join(
        f"2026-01-01T10:00:{index:02}Z node-a worker[101]: {payload}\n"
        for index, payload in enumerate(payloads)
    ).encode()
    source = source_for(content, display_name="events.log")
    batches = await collect(LogParser(), source, contexts_for(source, content)[1])
    analysis = await DeterministicStructureAnalyzer().analyze(stream(batches))
    assert batches[-1].manifest is not None
    return (
        StructureAnalysisRequest(
            source=source.ref,
            manifest=batches[-1].manifest,
            profile=analysis.profile,
            mode=SemanticParsingMode.LLM_FIRST,
            samples=samples_for(batches),
        ),
        batches,
    )


@pytest.mark.anyio
async def test_shapes_deduplicate_literal_scalar_paths_without_copying_values() -> None:
    row = {
        "requestID": "synthetic-private-value",
        "context": {"empty": None, "enabled": True, "status.code": 200},
        "arbitrary/поле": "another-private-value",
    }
    request, batches = await request_for(*(json.dumps(row) for _ in range(19)))
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    line_refs = [
        alias
        for alias, entry in catalog.entries.items()
        if entry.ref.kind is PhysicalObjectKind.LINE
    ]
    assert len(line_refs) == 19
    assert catalog.payload["log_json_shapes"] == [
        {
            "refs": line_refs,
            "selector": "log_json",
            "offset": 0,
            "scalar_paths": [
                ["arbitrary/поле"],
                ["context", "empty"],
                ["context", "enabled"],
                ["context", "status.code"],
                ["requestID"],
            ],
        }
    ]
    descriptor = canonical_json_value(catalog.payload["log_json_shapes"])
    assert "private-value" not in descriptor
    assert "timestamp" not in descriptor
    assert len(descriptor.encode()) < 400


@pytest.mark.anyio
async def test_shapes_keep_distinct_scope_and_exclude_unsampled_line_keys() -> None:
    request, batches = await request_for('{"first":null}', '{"second":true}')
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    assert catalog.payload["log_json_shapes"] == [
        {
            "refs": ["r0"],
            "selector": "log_json",
            "offset": 0,
            "scalar_paths": [["first"]],
        },
        {
            "refs": ["r1"],
            "selector": "log_json",
            "offset": 0,
            "scalar_paths": [["second"]],
        },
    ]
    first_line = next(
        sample
        for sample in request.samples
        if sample.source_ref.kind is PhysicalObjectKind.LINE
    )
    sampled = request.model_copy(update={"samples": (first_line,)})
    catalog = await prepare_samples(
        sampled, lambda: stream(batches), LLMStructurePolicy()
    )
    assert catalog.payload["log_json_shapes"] == [
        {
            "refs": ["r0"],
            "selector": "log_json",
            "offset": 0,
            "scalar_paths": [["first"]],
        }
    ]
    assert len(catalog.source_scope[PhysicalObjectKind.LINE]) == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        '{"broken":',
        '{"same":1,"same":2}',
        "plain message",
        '["array"]',
        '{"items":[1,2]}',
        '{"message":"keep","items":[{"nested":"value"}]}',
        '{"message":"keep","context":{"items":[]}}',
    ],
)
async def test_invalid_or_non_scalar_objects_do_not_advertise_json_paths(
    payload: str,
) -> None:
    request, batches = await request_for(payload)
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    assert "log_json_shapes" not in catalog.payload
    assert len(catalog.source_scope[PhysicalObjectKind.LINE]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        '{"first":1,"second":2}',
        '{"":1}',
        json.dumps({"x" * 257: 1}),
        '{"child":' * 30 + '{"leaf":1}' + "}" * 30,
    ],
)
async def test_shape_overflow_never_publishes_a_partial_descriptor(
    payload: str,
) -> None:
    request, batches = await request_for(payload)
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy(max_fields=1)
    )
    assert "log_json_shapes" not in catalog.payload


@pytest.mark.anyio
async def test_descriptor_bytes_remain_inside_existing_payload_budget() -> None:
    request, batches = await request_for('{"message":"synthetic"}')
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    limit = len(canonical_json_value(catalog.payload).encode()) - 1
    with pytest.raises(LLMProviderError) as caught:
        await prepare_samples(
            request,
            lambda: stream(batches),
            LLMStructurePolicy(max_payload_bytes=limit),
        )
    assert caught.value.error_code == LLMErrorCode.CONTEXT_LIMIT.value


@pytest.mark.anyio
async def test_unsampled_plain_line_is_not_declared_a_raw_event() -> None:
    request, batches = await request_for('{"message":"started"}', "GET /health 200")
    first = next(
        sample
        for sample in request.samples
        if sample.source_ref.kind is PhysicalObjectKind.LINE
    )
    catalog = await prepare_samples(
        request.model_copy(update={"samples": (first,)}),
        lambda: stream(batches),
        LLMStructurePolicy(),
    )
    assert "log_raw_records" not in catalog.payload
    assert catalog.payload["log_json_shapes"] == [
        {
            "refs": ["r0"],
            "selector": "log_json",
            "offset": 0,
            "scalar_paths": [["message"]],
        }
    ]


@pytest.mark.anyio
async def test_continuation_line_does_not_become_a_single_raw_event() -> None:
    content = b'2026-01-01T10:00:00Z node worker[101]: {"message":"failed"}\n  stack trace continuation\n'
    source = source_for(content, display_name="events.log")
    batches = await collect(LogParser(), source, contexts_for(source, content)[1])
    analysis = await DeterministicStructureAnalyzer().analyze(stream(batches))
    assert batches[-1].manifest is not None
    request = StructureAnalysisRequest(
        source=source.ref,
        manifest=batches[-1].manifest,
        profile=analysis.profile,
        mode=SemanticParsingMode.LLM_FIRST,
        samples=samples_for(batches),
    )
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    assert "log_raw_records" not in catalog.payload
    from structuraguard.structure import semantic_response_schema

    schema = semantic_response_schema()
    assert (
        schema.prepare_decoding(canonical_json_value(catalog.payload)).schema_json
        == schema.schema_json
    )
