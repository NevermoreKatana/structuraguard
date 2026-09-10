"""Поздний replay/cleanup failure не подтверждает уже выданный preview."""

from collections.abc import AsyncIterator

import pytest
from tests.fakes.documents import physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import context

from structuraguard.contracts import ExtractedBatch, PipelineStatus, SemanticParsingMode
from structuraguard.exceptions import ParseExecutionError
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", ("read", "malformed_batch", "cleanup"))
async def test_late_source_failure_never_commits_or_leaks_error(
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    batches = await physical(
        DelimitedTextParser(),
        b"name,n\nAda,1\nBob,2\nCara,3\n",
        batch_size=1,
    )
    delivered = False
    active = 0
    secret = "source-secret-canary-m06"

    class Replay(AsyncIterator[ExtractedBatch]):
        def __init__(self) -> None:
            nonlocal active
            self.index = 0
            self.closed = False
            active += 1

        async def __anext__(self) -> ExtractedBatch:
            if self.index == len(batches):
                raise StopAsyncIteration
            if delivered and failure == "read":
                raise OSError(secret)
            batch = batches[self.index]
            self.index += 1
            if delivered and failure == "malformed_batch":
                return batch.model_copy(
                    update={"batch_fingerprint": "sha256:" + "a" * 64}
                )
            return batch

        async def aclose(self) -> None:
            nonlocal active
            if not self.closed:
                self.closed = True
                active -= 1
                if delivered and failure == "cleanup":
                    raise OSError(secret)

    async with SemanticParsingSession(
        replay=Replay,
        policy=ParsingPolicy(
            mode=SemanticParsingMode.DETERMINISTIC, records_per_batch=1
        ),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        iterator = session.parse_semantically()
        first = await anext(iterator)
        assert first.records and not first.is_last and first.manifest is None
        delivered = True
        following = []
        with pytest.raises(ParseExecutionError) as raised:
            async for batch in iterator:
                following.append(batch)
        assert not any(batch.is_last or batch.manifest for batch in following)
        assert active == 0
        assert session.report is not None
        assert session.report.status is PipelineStatus.FAILED
        assert session.report.records >= 1 and session.report.llm_calls == 0
        assert session.report.normalized_fingerprint is None
        assert (
            secret
            not in str(raised.value) + session.report.canonical_json() + caplog.text
        )
