"""Регрессии финального review: saved review, общий deadline и terminal issues."""

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from tests.fakes.documents import chunks_for, document_script, physical, suggestion
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import (
    DocumentEntityProposal,
    DocumentEntitySuggestion,
    DocumentFieldProposal,
    ExtractedBatch,
    PipelineStatus,
    QuotedSpan,
    SemanticParsingMode,
)
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.llm import LLMBudget
from structuraguard.contracts.semantic import LLMStructurePolicy
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.exceptions import LLMProviderError, ParseExecutionError
from structuraguard.llm import FakeLLMProvider, NoLLMProvider, ScriptedResponse
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("unresolved", (False, True))
@pytest.mark.parametrize("mode", tuple(SemanticParsingMode))
async def test_saved_document_keeps_review_after_round_trip(
    unresolved: bool, mode: SemanticParsingMode
) -> None:
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    policy = ParsingPolicy()
    chunk = (await chunks_for(batches, policy))[0]
    proposal = suggestion(chunk)
    data = proposal.model_dump()
    data["unresolved_refs"] = ("r0",) if unresolved else ()
    proposal = DocumentEntitySuggestion.model_validate(data)
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=proposal.canonical_json()),), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        context=context(),
        provider=provider,
        scanner=Scanner(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as original:
        initial = [batch async for batch in original.parse_semantically()]
        assert original.report is not None and original.report.plan is not None
        plan = original.report.plan
        assert plan.final_assessment is not None
        assert bool(plan.final_assessment.penalty) is unresolved
        expected = (
            PipelineStatus.NEEDS_REVIEW if unresolved else PipelineStatus.COMPLETED
        )
        assert original.report.status is expected
    assert provider.call_count == 1
    disabled = NoLLMProvider()
    for _ in range(2):
        plan = type(plan).model_validate_json(plan.canonical_json())
        async with SemanticParsingSession(
            replay=lambda: stream(batches),
            policy=ParsingPolicy(mode=mode),
            context=context(),
            provider=disabled,
            clock=fixed_clock,
            timer=lambda: 0,
        ) as saved:
            output = [batch async for batch in saved.parse_semantically(plan=plan)]
            assert sum(len(b.records) for b in output) == sum(
                len(b.records) for b in initial
            )
            assert saved.report is not None
            assert saved.report.status is expected
            assert saved.report.llm_calls == 0
            if unresolved:
                assert not any(b.is_last or b.manifest is not None for b in output)
                assert saved.report.normalized_fingerprint is None
                assert saved.report.unresolved_refs and saved.report.unresolved_blocks
                assert saved.report.assessment is not None
                assert saved.report.assessment.confidence <= plan.confidence
            else:
                assert output == initial
                assert output[-1].is_last
            assert saved.report.plan is not None
            plan = saved.report.plan


@pytest.mark.parametrize("delay", ("coverage_read", "cumulative"))
async def test_saved_document_deadline_covers_coverage_replay(
    delay: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = ParsingPolicy(
        structural=LLMStructurePolicy(
            execution=ParsePlanOptions(
                source_limits=StructuralProfilingOptions(max_processing_seconds=1)
            )
        )
    )
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    provider = FakeLLMProvider(
        await document_script(batches, policy), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        context=context(),
        provider=provider,
        scanner=Scanner(),
        policy=policy,
        clock=fixed_clock,
        timer=lambda: 0,
    ) as original:
        plan = await original.create_parse_plan()
    assert plan is not None
    loop = asyncio.get_running_loop()
    now = loop.time()
    elapsed = Decimal(0)
    monkeypatch.setattr(loop, "time", lambda: now + float(elapsed))
    opened = 0
    closed: list[int] = []
    coverage_entered = asyncio.Event()

    async def replay() -> AsyncIterator[ExtractedBatch]:
        nonlocal opened, elapsed
        opened += 1
        index = opened
        try:
            if delay == "cumulative":
                elapsed += Decimal("0.4")
            # Первые два replay — M5 evidence и saved-plan validation; третий —
            # coverage. Event завершает тест и без SDK deadline, не скрывая дефект.
            if index == 3:
                coverage_entered.set()
                if delay == "coverage_read":
                    elapsed += Decimal(2)
                ready = asyncio.Event()
                loop.call_soon(ready.set)
                await ready.wait()
            for batch in batches:
                yield batch
        finally:
            closed.append(index)

    disabled = NoLLMProvider()
    async with SemanticParsingSession(
        replay=replay,
        context=context(),
        provider=disabled,
        policy=policy,
        clock=fixed_clock,
        timer=lambda: 0,
    ) as saved:
        with pytest.raises(LLMProviderError, match="LLM_TIMEOUT"):
            _ = [batch async for batch in saved.parse_semantically(plan=plan)]
        assert coverage_entered.is_set()
        assert opened == 3 and closed == [1, 2, 3]
        assert saved.report is not None
        assert saved.report.status is PipelineStatus.FAILED
        assert saved.report.normalized_fingerprint is None
        assert saved.report.llm_calls == 0


@pytest.mark.parametrize("limit", (1, 1000))
@pytest.mark.parametrize("failure", ("close", "cleanup"))
async def test_terminal_report_survives_saturated_issue_limit(
    limit: int, failure: str
) -> None:
    batches = await physical(
        MarkdownParser(),
        ("abcdefghijklmnopqrst" * (700 if limit == 1000 else 2)).encode(),
    )
    policy = ParsingPolicy(
        chunk_bytes=256,
        overlap_fragments=0,
        max_chunks=64,
        max_issues=limit,
        max_document_entities=1,
        records_per_batch=1,
        budget=LLMBudget(max_calls=64, max_tokens=1000000, max_time_ms=300000),
    )
    script = []
    for chunk in (await chunks_for(batches, policy))[: policy.max_chunks]:
        entities = []
        for i in range(16):
            span = QuotedSpan(
                ref="r0", start=i, end=i + 1, quote=chunk.fragments[0].text[i : i + 1]
            )
            entities.append(
                DocumentEntityProposal(
                    entity_id=f"e{i}",
                    entity_type="item",
                    anchor=span,
                    parent_entity_id=None,
                    fields=(
                        DocumentFieldProposal(
                            name="text", semantic_type="string", spans=(span,)
                        ),
                    ),
                )
            )
        script.append(
            ScriptedResponse(
                output_json=DocumentEntitySuggestion(
                    schema_version="1.0.0",
                    chunk_fingerprint=chunk.fingerprint,
                    entities=tuple(entities),
                    unresolved_refs=(),
                ).canonical_json()
            )
        )
    provider = FakeLLMProvider(tuple(script), clock=fixed_clock)
    armed = False
    active = 0

    class Replay(AsyncIterator[ExtractedBatch]):
        def __init__(self) -> None:
            nonlocal active
            active += 1
            self.index = 0
            self.closed = False

        async def __anext__(self) -> ExtractedBatch:
            if self.index == len(batches):
                raise StopAsyncIteration
            batch = batches[self.index]
            self.index += 1
            return batch

        async def aclose(self) -> None:
            nonlocal active
            if not self.closed:
                self.closed = True
                active -= 1
                if armed:
                    raise OSError("review-source-secret-canary")

    async with SemanticParsingSession(
        replay=Replay,
        context=context(),
        provider=provider,
        scanner=Scanner(),
        policy=policy,
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        analysis = await session.analyze_structure()
        assert analysis.plan is not None and len(analysis.issues) == limit
        iterator = session.parse_semantically()
        first = await anext(iterator)
        initial_report = session.report
        assert first.records and not first.is_last and initial_report is None
        if failure == "close":
            await iterator.aclose()
            expected = PipelineStatus.CANCELLED
            code = "SEMANTIC_CANCELLED"
        else:
            armed = True
            with pytest.raises(ParseExecutionError) as raised:
                _ = [batch async for batch in iterator]
            expected = PipelineStatus.FAILED
            code = raised.value.error_code
            assert "review-source-secret-canary" not in str(raised.value)
        assert active == 0
        assert session.report is not None
        assert session.report.status is expected
        assert session.report.normalized_fingerprint is None
        assert session.report.records == 1
        assert len(session.report.issues) <= limit
        assert code in {issue.code for issue in session.report.issues}
        assert session.report.llm_calls == provider.call_count
        assert "review-source-secret-canary" not in session.report.canonical_json()
