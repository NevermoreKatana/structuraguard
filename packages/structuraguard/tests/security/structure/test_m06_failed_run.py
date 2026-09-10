"""Ошибка после LLM attempt не позволяет перезапустить budget той же session."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from tests.fakes.documents import document_script, physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context

from structuraguard.contracts import ExtractedBatch, PipelineStatus
from structuraguard.contracts.llm import LLMBudget
from structuraguard.exceptions import LLMProviderError, ParseExecutionError
from structuraguard.llm import FakeLLMProvider
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", ("cleanup", "cancelled"))
async def test_failed_analysis_cannot_restart_call_budget_in_same_session(
    failure: str,
) -> None:
    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    policy = ParsingPolicy(
        budget=LLMBudget(max_calls=1, max_tokens=100000, max_time_ms=10000)
    )
    armed = True
    generated = False

    async def checkpoint() -> None:
        nonlocal generated
        generated = True
        if armed and failure == "cancelled":
            raise asyncio.CancelledError

    class Replay(AsyncIterator[ExtractedBatch]):
        def __init__(self) -> None:
            self.index = 0

        async def __anext__(self) -> ExtractedBatch:
            if self.index >= len(batches):
                raise StopAsyncIteration
            batch = batches[self.index]
            self.index += 1
            return batch

        async def aclose(self) -> None:
            if armed and generated and failure == "cleanup":
                raise OSError("source cleanup")

    provider = FakeLLMProvider(
        (await document_script(batches, policy)) * 2,
        clock=fixed_clock,
        before_response=checkpoint,
    )
    async with SemanticParsingSession(
        replay=Replay,
        provider=provider,
        scanner=Scanner(),
        context=context(),
        policy=policy,
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        with pytest.raises(
            ParseExecutionError if failure == "cleanup" else asyncio.CancelledError
        ):
            _ = [batch async for batch in session.parse_semantically()]
        assert provider.call_count == 1
        assert session.report is not None
        assert session.report.status is (
            PipelineStatus.FAILED if failure == "cleanup" else PipelineStatus.CANCELLED
        )
        assert (
            session.report.llm_calls == 1
            and session.report.normalized_fingerprint is None
        )
        armed = False
        with pytest.raises(LLMProviderError, match="LLM_REQUEST_INVALID"):
            await session.analyze_structure()
        assert provider.call_count == 1
