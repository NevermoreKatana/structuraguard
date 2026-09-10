"""Overlap identity, stable order, conflicts и конечные run budgets."""

import pytest
from tests.fakes.documents import chunks_for, document_script, physical, suggestion
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus
from structuraguard.contracts.llm import LLMBudget
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


async def test_overlap_dedup_stable_order_and_reproducible_report() -> None:
    batches = await physical(
        MarkdownParser(),
        b"Ada agrees.\n\nBob accepts.\n\nFee 1200.\n\nDate 2026-09-10.\n",
    )
    policy = ParsingPolicy(chunk_fragments=2, overlap_fragments=1)
    script = await document_script(batches, policy)
    assert len(script) == 3
    reports = []
    for _ in range(2):
        provider = FakeLLMProvider(script, clock=fixed_clock)
        async with SemanticParsingSession(
            replay=lambda: stream(batches),
            policy=policy,
            provider=provider,
            scanner=Scanner(),
            context=context(),
            clock=fixed_clock,
            timer=lambda: 0,
        ) as session:
            output = [batch async for batch in session.parse_semantically()]
            assert sum(len(batch.records) for batch in output) == 4
            values = [
                v.normalized_value.value
                for b in output
                for r in b.records
                for e in r.entities
                for v in e.values
            ]
            assert values == ["Ada", "Bob", "1200", "2026-09-10"]
            assert session.report is not None
            assert session.report.status is PipelineStatus.COMPLETED
            assert (
                session.report.assessment is not None
                and session.report.assessment.agreement == 1
            )
            reports.append(session.report.canonical_json())
    assert reports[0] == reports[1]


async def test_conflicting_overlap_is_review_and_does_not_duplicate_entity() -> None:
    batches = await physical(
        MarkdownParser(), b"Ada agrees.\n\nBob accepts.\n\nFee 1200.\n"
    )
    policy = ParsingPolicy(chunk_fragments=2, overlap_fragments=1)
    chunks = await chunks_for(batches, policy)
    first, second = suggestion(chunks[0]), suggestion(chunks[1])
    data = second.model_dump()
    data["entities"][0]["entity_type"] = "conflicting_type"
    altered = type(second).model_validate(data)
    provider = FakeLLMProvider(
        (
            ScriptedResponse(output_json=first.canonical_json()),
            ScriptedResponse(output_json=altered.canonical_json()),
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
        output = [batch async for batch in session.parse_semantically()]
        assert sum(len(batch.records) for batch in output) == 2
        assert not any(batch.is_last for batch in output)
        assert session.report is not None
        assert session.report.status is PipelineStatus.NEEDS_REVIEW
        assert session.report.unresolved_refs
        assert any(
            issue.code == "SEMANTIC_ENTITY_CONFLICT" for issue in session.report.issues
        )


@pytest.mark.parametrize("budget", ("calls", "chunks", "tokens", "entities"))
async def test_budgets_leave_explicit_unresolved_source(budget: str) -> None:
    batches = await physical(
        MarkdownParser(), b"Ada agrees.\n\nBob accepts.\n\nFee 1200.\n"
    )
    config: dict[str, object] = {"chunk_fragments": 2, "overlap_fragments": 1}
    if budget == "calls":
        config["budget"] = LLMBudget(max_calls=1, max_tokens=524288, max_time_ms=120000)
    elif budget == "tokens":
        config["budget"] = LLMBudget(max_calls=16, max_tokens=1, max_time_ms=120000)
    elif budget == "chunks":
        config["max_chunks"] = 1
    else:
        config["max_document_entities"] = 1
    policy = ParsingPolicy.model_validate(config)
    provider = FakeLLMProvider(
        await document_script(batches, policy), clock=fixed_clock
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
        output = [batch async for batch in session.parse_semantically()]
        assert not any(batch.is_last for batch in output)
        assert session.report is not None
        assert session.report.status is PipelineStatus.NEEDS_REVIEW
        assert session.report.unresolved_refs and session.report.unresolved_blocks
        assert provider.call_count <= (
            0 if budget == "tokens" else 1 if budget in {"calls", "chunks"} else 2
        )
