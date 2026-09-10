"""Lifecycle, saved plan replay и cancellation без успешного terminal."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
from tests.fakes.documents import document_script, physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.llm import FakeLLMProvider
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


async def test_saved_plan_executes_in_new_session_with_zero_llm() -> None:
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    provider = FakeLLMProvider(
        await document_script(batches, ParsingPolicy()), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        plan = await session.create_parse_plan()
        original = [batch async for batch in session.parse_semantically()]
    assert plan is not None
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as other:
        output = [batch async for batch in other.parse_semantically(plan=plan)]
        assert output == original
        assert other.report is not None and other.report.llm_calls == 0
        assert other.report.status is PipelineStatus.COMPLETED


async def test_cancelled_llm_keeps_safe_attempt_and_closes_replays() -> None:
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    entered = asyncio.Event()
    never = asyncio.Event()

    async def checkpoint() -> None:
        entered.set()
        await never.wait()

    provider = FakeLLMProvider(
        await document_script(batches, ParsingPolicy()),
        clock=fixed_clock,
        before_response=checkpoint,
    )
    active = 0

    async def replay() -> AsyncGenerator[ExtractedBatch, None]:
        nonlocal active
        active += 1
        try:
            for batch in batches:
                yield batch
        finally:
            active -= 1

    async with SemanticParsingSession(
        replay=replay,
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        iterator = session.parse_semantically()
        task = asyncio.create_task(anext(iterator))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert active == 0
        assert session.report is not None
        assert session.report.status is PipelineStatus.CANCELLED
        assert session.report.llm_calls == 1
        assert session.report.normalized_fingerprint is None


async def test_close_early_stream_is_cancelled_and_not_reusable() -> None:
    from structuraguard.contracts import SemanticParsingMode
    from structuraguard.exceptions import LLMProviderError
    from structuraguard.parsers.builtin import DelimitedTextParser

    batches = await physical(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\nCara,3\n")
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(
            mode=SemanticParsingMode.DETERMINISTIC, records_per_batch=1
        ),
        context=context(),
        clock=fixed_clock,
    ) as session:
        iterator = session.parse_semantically()
        first = await anext(iterator)
        assert not first.is_last
        await iterator.aclose()
        assert session.report is not None
        assert session.report.status is PipelineStatus.CANCELLED
        assert session.report.normalized_fingerprint is None
        with pytest.raises(LLMProviderError):
            session.parse_semantically()


async def test_deterministic_paths_are_not_subject_to_llm_name_caps() -> None:
    import json

    from structuraguard.contracts import SemanticParsingMode
    from structuraguard.parsers.builtin import JsonDocumentParser

    content = json.dumps([{"x" * 300: "Ada"}, {"x" * 300: "Bob"}]).encode()
    batches = await physical(JsonDocumentParser(), content)
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        context=context(),
        clock=fixed_clock,
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        assert output[-1].is_last
        assert session.report is not None and session.report.llm_calls == 0


async def test_closing_after_received_terminal_preserves_completed_report() -> None:
    from structuraguard.parsers.builtin import DelimitedTextParser

    batches = await physical(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    async with SemanticParsingSession(
        replay=lambda: stream(batches), context=context(), clock=fixed_clock
    ) as session:
        iterator = session.parse_semantically()
        terminal = await anext(iterator)
        assert terminal.is_last
        await iterator.aclose()
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED
