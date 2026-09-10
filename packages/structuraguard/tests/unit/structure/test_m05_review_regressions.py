"""Регрессии потери scope и ошибочных числовых hints из финального review M5."""

import pytest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_analysis import analyze_content
from tests.unit.structure.test_execution import execute, prepared

from structuraguard.contracts.parsing import StructureNeedsReview, StructurePlanCreated
from structuraguard.contracts.source import ExtractedBlock, ExtractedBlockKind
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    LogParser,
    PlainTextParser,
)
from structuraguard.parsers.builtin._common import _build_batch
from structuraguard.structure import DeterministicStructureAnalyzer
from structuraguard.structure._samples import primitive


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "offset", "tail"),
    [
        (ExtractedBlockKind.PARAGRAPH, 2, b"Order A\nOrder B\n"),
        (ExtractedBlockKind.LINE, 2, b"Order A\nOrder B\n"),
        (ExtractedBlockKind.LINE, 2, b"INFO 1\nINFO 2\n"),
        (ExtractedBlockKind.LINE, 0, b"Order A\nOrder B\n"),
        (ExtractedBlockKind.PARAGRAPH, 0, b"INFO 1\nINFO 2\n"),
    ],
)
async def test_independent_blocks_remain_candidates_beside_physical_lines(
    kind: ExtractedBlockKind, offset: int, tail: bytes
) -> None:
    content = b"INFO 1\nINFO 2\n" + tail
    source = source_for(content, display_name="mixed.txt")
    original = (
        await collect(PlainTextParser(), source, contexts_for(source, content)[1])
    )[0]
    blocks = tuple(
        ExtractedBlock(
            block_id=f"block-{i + 1}",
            kind=kind,
            order=i,
            location=original.lines[offset + i].location,
            text=line.text,
        )
        for i, line in enumerate(original.lines[2:])
    )
    batch = _build_batch(
        source=source,
        run_id=original.extraction_id,
        adapter_id="test.mixed",
        version="1.0.0",
        batch_index=0,
        lines=original.lines[:2],
        blocks=blocks,
        record_count=4,
        prior_summaries=(),
        prior_indexed_refs=(),
        options_fingerprint="sha256:" + "a" * 64,
        is_last=True,
    )
    assert batch.manifest is not None
    batch.manifest.validate_batches((batch,))
    result = await DeterministicStructureAnalyzer().analyze(batch)
    assert result.profile.coverage is not None
    assert result.profile.coverage.complete
    assert isinstance(result, StructureNeedsReview)
    assert {candidate.plan_kind for candidate in result.candidates} == {
        "log",
        "document",
    }
    document = next(c for c in result.candidates if c.plan_kind == "document")
    assert {ref.local_id for ref in document.evidence} == {b.block_id for b in blocks}


@pytest.mark.anyio
@pytest.mark.parametrize("batch_size", [1, 2])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n"])
async def test_verified_multiline_log_duplicates_keep_one_executable_plan(
    batch_size: int, ending: bytes
) -> None:
    lines = (
        b"2026-09-10T12:00:00Z INFO user=1 started",
        b"  detail one",
        b"2026-09-10T12:01:00Z ERROR user=2 failed",
        b"  detail two",
    )
    request, batches = await prepared(
        LogParser(), ending.join(lines), batch_size=batch_size
    )
    assert request.plan.kind == "log"
    output = await execute(request, batches)
    origins = [
        origin.raw_value.value
        for batch in output
        for record in batch.records
        for entity in record.entities
        for value in entity.values
        for origin in value.origins
    ]
    assert origins == [line.decode() for line in lines]


@pytest.mark.parametrize("value", ["-+1", "-+1.5", "+-1", "--1", "++1.5"])
def test_multiple_signs_are_not_numeric_hints(value: str) -> None:
    assert primitive(value) == "string"


@pytest.mark.anyio
@pytest.mark.parametrize("value", ["-+1", "-+1.5"])
async def test_invalid_signed_value_does_not_crash_footer_analysis(value: str) -> None:
    result = await analyze_content(
        DelimitedTextParser(), f"name,n\nAda,{value}\nBob,2\nTotal,3\n".encode()
    )
    assert result.kind in {"needs_review", "needs_semantic_analysis"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "values", [b"+1\nBob,+2\nTotal,+3\n", b"-1.5\nBob,+2.5\nTotal,+1.0\n"]
)
async def test_single_signed_numbers_still_support_verified_totals(
    values: bytes,
) -> None:
    result = await analyze_content(DelimitedTextParser(), b"name,n\nAda," + values)
    assert isinstance(result, StructurePlanCreated)
    assert result.plan.kind == "tabular"
    assert result.plan.footer_start_row == 3
