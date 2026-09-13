"""Source byte cap не зависит от разбиения caller stream на chunks."""

import asyncio
from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.pipeline import FakeDatabase, FakeParser, Stream, defaults, engine

from structuraguard.contracts import PipelineStatus as S
from structuraguard.pipeline import SourceRequest


class ChunkedStream(Stream):
    def __init__(self, data: bytes, chunk_size: int) -> None:
        super().__init__(data)
        self.chunk_size = chunk_size

    async def read(self, size: int) -> bytes:
        return await super().read(min(size, self.chunk_size))


@settings(max_examples=20, deadline=None, derandomize=True)
@given(
    limit=st.integers(min_value=8, max_value=512),
    chunk_size=st.integers(min_value=1, max_value=1024),
)
def test_oversized_source_stops_before_detection(limit: int, chunk_size: int) -> None:
    async def check() -> None:
        db, parser = FakeDatabase(), FakeParser()
        stream = ChunkedStream(b" " * (limit + 1), chunk_size)
        result = await engine(
            dependencies=replace(defaults(db), max_snapshot_bytes=limit),
            parser=parser,
        ).ingest(SourceRequest(stream=stream))
        assert result.status is S.REJECTED_SECURITY
        assert result.errors[-1].code == "SECURITY_LIMIT_EXCEEDED"
        assert parser.probes == parser.parses == 0
        assert db.inspector.calls == db.writes == 0
        assert result.source_report is None and result.load_report is None
        assert stream.offset <= limit + 1

    asyncio.run(check())
