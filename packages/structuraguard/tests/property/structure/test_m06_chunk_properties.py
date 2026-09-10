"""Unicode и overlap сохраняют точные исходные offsets при bounded chunks."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.documents import physical
from tests.unit.structure.test_execution import stream

from structuraguard.contracts._base import canonical_json_value
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.structure.chunking import ChunkedSource
from structuraguard.structure.text_sources import text_sources


@settings(max_examples=20, derandomize=True, deadline=None)
@given(
    text=st.text(alphabet='аб漢🙂e\u0301"\\\t', min_size=1, max_size=100),
    chunk_bytes=st.sampled_from((256, 512, 1024)),
    overlap=st.integers(0, 2),
)
def test_unicode_chunks_reconstruct_source_without_gaps(
    text: str,
    chunk_bytes: int,
    overlap: int,
) -> None:
    async def check() -> None:
        batches = await physical(
            MarkdownParser(), ("Начало " + text * 4 + " конец").encode()
        )
        manifest = batches[-1].manifest
        assert manifest is not None
        policy = ParsingPolicy(
            chunk_bytes=chunk_bytes, chunk_fragments=3, overlap_fragments=overlap
        )
        source = ChunkedSource(manifest, policy)
        chunks = [chunk async for chunk in source.chunks(lambda: stream(batches))]
        original = {
            item.ref: item.text for batch in batches for item in text_sources(batch)
        }
        unique = {}
        for chunk in chunks:
            assert len(chunk.fragments) <= policy.chunk_fragments
            assert (
                len(
                    canonical_json_value(
                        [f.payload(f"r{i}") for i, f in enumerate(chunk.fragments)]
                    ).encode()
                )
                <= chunk_bytes
            )
            for fragment in chunk.fragments:
                assert (
                    fragment.text
                    == original[fragment.ref][
                        fragment.start : fragment.start + len(fragment.text)
                    ]
                )
                unique[fragment.ref, fragment.start] = fragment.text
        for ref, raw in original.items():
            parts = sorted(
                (start, value)
                for (part_ref, start), value in unique.items()
                if part_ref == ref
            )
            cursor = 0
            for start, value in parts:
                assert start == cursor
                cursor += len(value)
            assert "".join(value for _, value in parts) == raw
        assert source.complete and source.omitted == 0
        assert len({chunk.fingerprint for chunk in chunks}) == len(chunks)

    asyncio.run(check())
