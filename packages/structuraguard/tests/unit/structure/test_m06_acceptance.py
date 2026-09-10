"""Недостающие наблюдаемые границы K1/K3: пустота и размер batches."""

import pytest
from tests.fakes.documents import physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context, encoded, scenario
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus, SemanticParsingMode
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.parsers.builtin import DelimitedTextParser, MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("mode", tuple(SemanticParsingMode))
async def test_empty_document_has_no_plan_calls_or_success(
    mode: SemanticParsingMode,
) -> None:
    batches = await physical(MarkdownParser(), b"")
    provider = FakeLLMProvider((), clock=fixed_clock)
    scanner = Scanner()
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(mode=mode),
        provider=provider,
        scanner=scanner,
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        assert [batch async for batch in session.parse_semantically()] == []
        report = session.report
        assert report is not None and report.status is PipelineStatus.NEEDS_REVIEW
        assert report.plan is None
        assert report.parse_plan_fingerprint is None
        assert report.normalized_fingerprint is None
        assert report.records == report.provenance_coverage == report.llm_calls == 0
        assert [issue.code for issue in report.issues] == ["SOURCE_EMPTY"]
    assert provider.call_count == 0 and scanner.requests == []


@pytest.mark.parametrize("rows", (4, 40))
@pytest.mark.parametrize("physical_batch_size", (1, 7, 100))
async def test_table_row_and_batch_growth_keeps_one_plan_call(
    rows: int,
    physical_batch_size: int,
) -> None:
    content = ("name,n\n" + "".join(f"person{i},{i}\n" for i in range(rows))).encode()
    _, batches, proposed = await scenario(
        DelimitedTextParser(),
        content,
        batch_size=physical_batch_size,
    )
    for output_batch_size in (1, 100):
        provider = FakeLLMProvider(
            (ScriptedResponse(output_json=encoded(proposed)),),
            clock=fixed_clock,
        )
        scanner = Scanner()
        policy = ParsingPolicy(
            mode=SemanticParsingMode.LLM_FIRST, records_per_batch=output_batch_size
        )
        async with SemanticParsingSession(
            replay=lambda: stream(batches),
            provider=provider,
            scanner=scanner,
            policy=policy,
            context=context(),
            clock=fixed_clock,
            timer=lambda: 0,
        ) as session:
            output = [batch async for batch in session.parse_semantically()]
            assert session.report is not None
            assert session.report.status is PipelineStatus.COMPLETED, (
                session.report.issues
            )
            assert output[-1].is_last
            assert [
                tuple(
                    value.normalized_value.value
                    for entity in record.entities
                    for value in entity.values
                )
                for batch in output
                for record in batch.records
            ] == [(f"person{i}", str(i)) for i in range(rows)]
            assert provider.call_count == session.report.llm_calls == 1
            assert len(scanner.requests) == 1
            assert (
                len(scanner.requests[0].payload_json.encode())
                <= policy.structural.max_payload_bytes
            )
            assert session.report.provenance_coverage == 1
