"""Hybrid выполняет весь semantic flow с настоящими M4/M5 и fake LLM."""

import pytest
from tests.fakes.documents import physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context, encoded, scenario
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus, SemanticParsingMode
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.llm import (
    FakeLLMProvider,
    NoLLMProvider,
    ScriptedFailure,
    ScriptedResponse,
)
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    LogParser,
    MarkdownParser,
)
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession


@pytest.mark.anyio
async def test_csv_regions_use_deterministic_plan_without_llm() -> None:
    from structuraguard.parsers.builtin import DelimitedTextParser

    request, batches, _ = await scenario(
        DelimitedTextParser(), b"Report\nname,amount\nAda,12\nBob,20\nTotal,32\n"
    )
    provider = FakeLLMProvider((), clock=fixed_clock)
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        assert output[-1].is_last
        assert sum(len(batch.records) for batch in output) == 2
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED
        assert session.report.llm_calls == provider.call_count == 0
        assert session.report.plan is not None
        assert session.report.provenance_coverage == 1
        assert session.report.source_fingerprint == request.source.source_fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize("mode", tuple(SemanticParsingMode))
async def test_modes_control_calls_and_plan_reuse(mode: SemanticParsingMode) -> None:
    _, batches, proposed = await scenario(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n"
    )
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(proposed)),), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(mode=mode),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        first = await session.create_parse_plan()
        assert await session.create_parse_plan() == first
        output = [batch async for batch in session.parse_semantically()]
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED, session.report.issues
        assert output[-1].is_last
        assert provider.call_count == int(mode is SemanticParsingMode.LLM_FIRST)
        assert session.report.assessment is not None
        assert session.report.assessment.confidence != proposed["self_confidence"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content,records",
    [
        (
            JsonDocumentParser(),
            b'[{"id":1,"items":[{"sku":"a"},{"sku":"b"}]},{"id":2,"items":[{"sku":"c"}]}]',
            2,
        ),
        (
            LogParser(),
            b"2026-09-10T12:00:00Z INFO user=1 started\n  detail one\n2026-09-10T12:01:00Z ERROR user=2 failed\n  detail two\n",
            2,
        ),
    ],
)
async def test_nested_json_and_mixed_log_preserve_records(
    parser: object, content: bytes, records: int
) -> None:
    from structuraguard.ports.parser import Parser

    assert isinstance(parser, Parser)
    _, batches, proposed = await scenario(parser, content)
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(proposed)),), clock=fixed_clock
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
        output = [batch async for batch in session.parse_semantically()]
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED, session.report.issues
        assert sum(len(batch.records) for batch in output) == records
        assert provider.call_count == 1
        assert all(
            value.source_refs
            for batch in output
            for record in batch.records
            for entity in record.entities
            for value in entity.values
        )
        if isinstance(parser, JsonDocumentParser):
            assert (
                sum(
                    bool(e.parent_entity_id)
                    for batch in output
                    for r in batch.records
                    for e in r.entities
                )
                == 3
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code", (LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT, LLMErrorCode.UNAVAILABLE)
)
async def test_fallback_is_explicit_preview_without_terminal(code: object) -> None:
    _, batches, _ = await scenario(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    provider = FakeLLMProvider(
        (ScriptedFailure.model_validate({"code": code}),), clock=fixed_clock
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
        output = [batch async for batch in session.parse_semantically()]
        assert sum(len(b.records) for b in output) == 2
        assert all(not b.is_last and b.manifest is None for b in output)
        assert session.report is not None
        assert session.report.status is PipelineStatus.NEEDS_REVIEW
        assert session.report.normalized_fingerprint is None
        assert any(issue.code == code for issue in session.report.issues)
        assert session.report.provider_metadata[0].error_code == code


@pytest.mark.anyio
async def test_deterministic_document_does_not_need_provider_and_no_llm_is_explicit() -> (
    None
):
    batches = await physical(MarkdownParser(), b"Ada and Bob agree for 1200.\n")
    for mode in (SemanticParsingMode.DETERMINISTIC, SemanticParsingMode.LLM_ASSISTED):
        async with SemanticParsingSession(
            replay=lambda: stream(batches),
            policy=ParsingPolicy(mode=mode),
            provider=NoLLMProvider(),
            scanner=Scanner(),
            context=context(),
            clock=fixed_clock,
            timer=lambda: 0,
        ) as session:
            _ = [batch async for batch in session.parse_semantically()]
            assert session.report is not None
            assert session.report.llm_calls == 0
            if mode is SemanticParsingMode.LLM_ASSISTED:
                assert session.report.status is PipelineStatus.NEEDS_REVIEW
