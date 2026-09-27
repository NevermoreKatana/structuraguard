"""LLM → закрытый JSON selector → replay validation/execution с LINE provenance."""

import json
from functools import partial

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, Validator, context, samples_for, scenario
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import execute, prepared, stream

from structuraguard.contracts.common import (
    PhysicalObjectKind,
    SemanticParsingMode,
    ValidationDecision,
)
from structuraguard.contracts.parsing import (
    LogJsonSelector,
    LogParsePlan,
    ParseField,
    ParsePlanValidationRequest,
    StructureAnalysisRequest,
    StructurePlanCreated,
)
from structuraguard.contracts.semantic import (
    LLMStructurePolicy,
    LLMStructureSuggestion,
    SemanticEntityProposal,
    SemanticFieldProposal,
    SemanticPathStep,
    SemanticPlanProposal,
    SemanticSelector,
)
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.parsers.builtin import LogParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.structure import (
    DeterministicStructureAnalyzer,
    LLMStructureAnalyzer,
    ParsePlanValidator,
)
from structuraguard.structure.semantic_samples import prepare_samples


def syslog(*payloads: str) -> bytes:
    return "".join(
        f"2026-01-01T10:00:0{i + 1}Z node-a worker[101]: {payload}\n"
        for i, payload in enumerate(payloads)
    ).encode()


@pytest.mark.anyio
async def test_llm_first_reports_rejected_json_selector_at_the_source_line() -> None:
    _, batches, proposed = await scenario(
        LogParser(), syslog('{"message":"one"}', '{"message":"two"}')
    )
    suggestion = LLMStructureSuggestion.model_validate(proposed)
    assert suggestion.plan is not None
    field = suggestion.plan.fields[0].model_copy(
        update={
            "selector": SemanticSelector(
                kind="log_json",
                offset=0,
                index=None,
                path=(SemanticPathStep(operation="key", name="missing", occurrence=0),),
                value_source=None,
                delimiter=None,
                target=None,
                key_equals=None,
            )
        }
    )
    proposal = suggestion.plan.model_copy(update={"fields": (field,)})
    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json=suggestion.model_copy(
                    update={"plan": proposal}
                ).canonical_json()
            ),
        ),
        clock=fixed_clock,
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(mode=SemanticParsingMode.LLM_FIRST),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        analysis = await session.analyze_structure()
        assert any(issue.code == "LLM_SCHEMA_VIOLATION" for issue in analysis.issues)
        assert analysis.execution_issue is not None
        assert analysis.execution_issue.reason == "log_json_path_missing"
        assert analysis.execution_issue.field_id == field.field_id
        assert analysis.execution_issue.source_ref is not None
        assert analysis.execution_issue.source_ref.kind is PhysicalObjectKind.LINE


@pytest.mark.anyio
async def test_llm_json_selection_keeps_clean_values_and_original_line() -> None:
    rows = [
        {
            "requestID": "00000000-0000-4000-8000-000000000001",
            "date": "2026-01-01",
            "message": 'Hello, "Анна"; path C:\\tmp\nnext line',
            "level": "INFO",
            "context": {"empty": None},
        },
        {
            "requestID": "00000000-0000-4000-8000-000000000002",
            "date": "2026-01-02",
            "message": "Another message, with spaces",
            "level": "ERROR",
            "context": {"empty": None},
        },
    ]
    content = syslog(*(json.dumps(row, ensure_ascii=False) for row in rows))
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
    scope = tuple(
        alias
        for alias, entry in catalog.entries.items()
        if entry.ref.kind is PhysicalObjectKind.LINE
    )
    paths = (("requestID",), ("date",), ("message",), ("level",), ("context", "empty"))
    names = ("request_id", "date", "message", "level", "empty")
    fields = tuple(
        SemanticFieldProposal(
            field_id=name,
            semantic_name=name,
            semantic_type="string",
            locale_hint=None,
            source_refs=scope,
            selector=SemanticSelector(
                kind="log_json",
                index=None,
                offset=0,
                delimiter=None,
                path=tuple(
                    SemanticPathStep(operation="key", name=key, occurrence=0)
                    for key in path
                ),
                value_source=None,
                target=None,
                key_equals=None,
            ),
        )
        for name, path in zip(names, paths, strict=True)
    )
    proposal = SemanticPlanProposal(
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
                entity_id="events",
                entity_type="events",
                parent_entity_id=None,
                field_ids=names,
                path=(),
                records=tuple((ref,) for ref in scope),
            ),
        ),
    )
    suggestion = LLMStructureSuggestion(
        schema_version="1.0.0",
        decision="plan",
        candidate_ids=(),
        self_confidence=0.99,
        plan=proposal,
    )
    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json=suggestion.model_copy(
                    update={"plan": proposal}
                ).canonical_json()
            ),
        ),
        clock=fixed_clock,
    )
    result = await LLMStructureAnalyzer(
        provider=provider, scanner=Scanner(), validator=Validator(), context=context()
    ).propose(request, replay=partial(stream, batches))
    assert isinstance(result, StructurePlanCreated)
    assert all(
        isinstance(field.selector, LogJsonSelector) for field in result.plan.fields
    )
    validation = ParsePlanValidationRequest(
        plan=result.plan,
        source=request.source,
        manifest=request.manifest,
        profile=request.profile,
    )
    output = await execute(validation, batches)
    records = [record for batch in output for record in batch.records]
    assert len(records) == 2
    for index, record in enumerate(records):
        values = record.entities[0].values
        assert [value.normalized_value.value for value in values] == [
            rows[index]["requestID"],
            rows[index]["date"],
            rows[index]["message"],
            rows[index]["level"],
            None,
        ]
        for value in values:
            assert value.origins[0].source_ref.kind is PhysicalObjectKind.LINE
            assert (
                value.origins[0].raw_value.value == content.decode().splitlines()[index]
            )
            assert value.transformations == ("select_json",)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload,reason",
    [
        ('{"message":"secret-canary",}', "LOG_JSON_MALFORMED"),
        ('{"message":"first","message":"secret-canary"}', "LOG_JSON_MALFORMED"),
        ('{"message":NaN}', "LOG_JSON_MALFORMED"),
        ('{"message":"ok"} trailing-secret-canary', "LOG_JSON_MALFORMED"),
        ('{"other":"secret-canary"}', "LOG_JSON_PATH_MISSING"),
        ('{"message":{"secret-canary":1}}', "LOG_JSON_SCALAR_REQUIRED"),
    ],
)
async def test_invalid_json_plan_is_rejected_with_source_reference(
    payload: str, reason: str
) -> None:
    request, batches = await prepared(LogParser(), syslog(payload, payload))
    assert isinstance(request.plan, LogParsePlan)
    plan = LogParsePlan.model_validate(
        {
            **request.plan.model_dump(),
            "schema_version": "1.1.0",
            "analysis": None,
            "fingerprint": "sha256:" + "0" * 64,
            "fields": (
                ParseField(
                    field_id="message",
                    semantic_name="message",
                    semantic_type="string",
                    source_refs=request.plan.line_refs,
                    selector=LogJsonSelector(path=("message",)),
                ),
            ),
            "entities": tuple(
                entity.model_copy(update={"field_ids": ("message",)})
                for entity in request.plan.entities
            ),
        }
    )
    result = await ParsePlanValidator().validate_source(
        request.model_copy(update={"plan": plan}), stream(batches)
    )
    assert result.decision is ValidationDecision.REJECTED
    assert result.issues[0].message_key == reason
    assert result.issues[0].source_refs == ()
    assert result.execution_issue is not None
    assert result.execution_issue.source_ref == plan.line_refs[0]
    assert result.execution_issue.field_id == "message"
    assert "secret-canary" not in result.model_dump_json()
