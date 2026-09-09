"""Runtime-проверки целостности physical extraction schema 1.1."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pytest
from tests.fakes.parsers import (
    FakeParser,
    FakeSourceReader,
    valid_extracted_batches,
)

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedDatasetManifest,
    ExtractedLine,
    ExtractedSourceIndex,
    LineRangeLocation,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    ProducerMetadata,
    SourceArtifact,
)
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.ports.source import ParseContext, ProbeContext

_SOURCE_FINGERPRINT = "sha256:" + "a" * 64
_PLACEHOLDER_FINGERPRINT = "sha256:" + "0" * 64
_OPTIONS_FINGERPRINT = "sha256:" + "b" * 64
_SCHEMA_VERSION = "1.1.0"
_EXTRACTION_ID = "extraction-1"
_PARSER_ID = "fake.parser"
_PARSER_VERSION = "1.0.0"
_MAX_INDEXED_REFS = 10_000


def _source() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="source-1",
        display_name="sample.fake",
        media_type="application/x-fake",
        size_bytes=32,
        source_fingerprint=_SOURCE_FINGERPRINT,
    )


def _contexts(
    *,
    max_records: int = 16,
    max_physical_objects: int = 16,
) -> tuple[ProbeContext, ParseContext]:
    reader = FakeSourceReader(_SOURCE_FINGERPRINT, content=b"fake source")
    return (
        ProbeContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_probe_bytes=64,
        ),
        ParseContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_bytes=128,
            max_records=max_records,
            max_nesting_depth=4,
            max_physical_objects=max_physical_objects,
        ),
    )


def _probe(source: SourceArtifact) -> ProbeResult:
    return ProbeResult(
        source=source.ref,
        adapter_id=_PARSER_ID,
        adapter_version=_PARSER_VERSION,
        supported=True,
        confidence=Decimal("1"),
        detected_media_type="application/x-fake",
        format_id="fake",
        signals=(
            ProbeSignal(
                kind=ProbeSignalKind.SIGNATURE,
                outcome=ProbeSignalOutcome.MATCH,
            ),
        ),
    )


def _line(source: SourceArtifact, *, line_number: int) -> ExtractedLine:
    return ExtractedLine(
        line_id=f"line-{line_number}",
        line_number=line_number,
        text=f"raw-{line_number}",
        location=LineRangeLocation(
            source=source.ref,
            line_start=line_number,
            line_end=line_number,
        ),
    )


def _line_ref(*, batch_index: int, line_number: int) -> PhysicalSourceRef:
    return PhysicalSourceRef(
        extraction_id=_EXTRACTION_ID,
        batch_index=batch_index,
        kind=PhysicalObjectKind.LINE,
        local_id=f"line-{line_number}",
    )


def _canonical_batch_fingerprint(
    draft: ExtractedBatch,
    *,
    is_last: bool,
) -> str:
    payload = draft.model_dump(
        mode="python",
        round_trip=True,
        exclude={"batch_fingerprint", "manifest"},
    )
    payload["is_last"] = is_last
    return canonical_sha256_value(payload)


def _draft_batch(
    source: SourceArtifact,
    *,
    batch_index: int,
    lines: tuple[ExtractedLine, ...],
    indexed_refs: tuple[PhysicalSourceRef, ...],
) -> ExtractedBatch:
    return ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=_EXTRACTION_ID,
        batch_index=batch_index,
        source=source.ref,
        parser_id=_PARSER_ID,
        parser_version=_PARSER_VERSION,
        batch_fingerprint=_PLACEHOLDER_FINGERPRINT,
        lines=lines,
        record_count=len(lines),
        indexed_refs=indexed_refs,
    )


def _canonical_manifest(
    source: SourceArtifact,
    *,
    summaries: tuple[ExtractedBatchSummary, ...],
    source_index: ExtractedSourceIndex,
) -> ExtractedDatasetManifest:
    record_count = sum(summary.record_count or 0 for summary in summaries)
    seed = ExtractedDatasetManifest(
        schema_version=_SCHEMA_VERSION,
        source=source.ref,
        extraction_id=_EXTRACTION_ID,
        parser_id=_PARSER_ID,
        parser_version=_PARSER_VERSION,
        producer=ProducerMetadata(
            component_id=_PARSER_ID,
            component_version=_PARSER_VERSION,
            sdk_version="0.3.0",
        ),
        parser_options_fingerprint=_OPTIONS_FINGERPRINT,
        batches=summaries,
        extraction_fingerprint=_PLACEHOLDER_FINGERPRINT,
        source_index=source_index,
        record_count=record_count,
    )
    fingerprint = canonical_sha256_value(
        seed,
        exclude_top_level=frozenset({"extraction_fingerprint"}),
    )
    return seed.model_copy(update={"extraction_fingerprint": fingerprint})


def _canonical_batches(
    source: SourceArtifact,
    *,
    lines_by_batch: tuple[tuple[ExtractedLine, ...], ...],
    indexed_refs_by_batch: tuple[tuple[PhysicalSourceRef, ...], ...] | None = None,
    manifest_refs: Iterable[PhysicalSourceRef] | None = None,
) -> tuple[ExtractedBatch, ...]:
    if indexed_refs_by_batch is None:
        indexed_refs_by_batch = tuple(() for _ in lines_by_batch)
    drafts = tuple(
        _draft_batch(
            source,
            batch_index=batch_index,
            lines=lines,
            indexed_refs=indexed_refs_by_batch[batch_index],
        )
        for batch_index, lines in enumerate(lines_by_batch)
    )
    fingerprints = tuple(
        _canonical_batch_fingerprint(
            draft,
            is_last=batch_index == len(drafts) - 1,
        )
        for batch_index, draft in enumerate(drafts)
    )
    summaries = tuple(
        draft.model_copy(
            update={"batch_fingerprint": fingerprints[batch_index]}
        ).to_summary()
        for batch_index, draft in enumerate(drafts)
    )
    indexed = (
        tuple(manifest_refs)
        if manifest_refs is not None
        else tuple(ref for refs in indexed_refs_by_batch for ref in refs)
    )
    manifest = _canonical_manifest(
        source,
        summaries=summaries,
        source_index=ExtractedSourceIndex(refs=indexed),
    )
    return tuple(
        ExtractedBatch(
            **draft.model_dump(
                mode="python",
                round_trip=True,
                exclude={"batch_fingerprint", "is_last", "manifest"},
            ),
            batch_fingerprint=fingerprints[batch_index],
            is_last=batch_index == len(drafts) - 1,
            manifest=manifest if batch_index == len(drafts) - 1 else None,
        )
        for batch_index, draft in enumerate(drafts)
    )


@pytest.mark.anyio
async def test_registry_accepts_canonical_schema_1_1_fingerprints() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    batches = _canonical_batches(
        source,
        lines_by_batch=((_line(source, line_number=1),),),
    )
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=batches))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == batches[0]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    assert stream.completed is True


@pytest.mark.anyio
async def test_schema_1_1_rejects_batch_fingerprint_for_forged_physical_payload() -> (
    None
):
    source = _source()
    probe_context, parse_context = _contexts()
    canonical = _canonical_batches(
        source,
        lines_by_batch=((_line(source, line_number=1),),),
    )[0]
    forged_line = canonical.lines[0].model_copy(update={"text": "forged"})
    forged = canonical.model_copy(update={"lines": (forged_line,)})
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_fingerprint_mismatch"


@pytest.mark.anyio
async def test_schema_1_1_rejects_forged_extraction_fingerprint() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    canonical = _canonical_batches(
        source,
        lines_by_batch=((_line(source, line_number=1),),),
    )[0]
    assert canonical.manifest is not None
    forged_manifest = canonical.manifest.model_copy(
        update={"extraction_fingerprint": "sha256:" + "f" * 64}
    )
    forged = canonical.model_copy(update={"manifest": forged_manifest})
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "extraction_fingerprint_mismatch"


@pytest.mark.anyio
async def test_schema_1_1_requires_exact_cumulative_indexed_refs_in_manifest() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    first_ref = _line_ref(batch_index=0, line_number=1)
    second_ref = _line_ref(batch_index=1, line_number=2)
    batches = _canonical_batches(
        source,
        lines_by_batch=(
            (_line(source, line_number=1),),
            (_line(source, line_number=2),),
        ),
        indexed_refs_by_batch=((first_ref,), (second_ref,)),
        manifest_refs=(first_ref,),
    )
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=batches))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == batches[0]
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "manifest_source_index"


@pytest.mark.anyio
async def test_schema_1_1_caps_indexed_refs_across_batches() -> None:
    source = _source()
    probe_context, parse_context = _contexts(
        max_records=_MAX_INDEXED_REFS + 1,
        max_physical_objects=_MAX_INDEXED_REFS + 1,
    )
    first_count = 5_001
    first_lines = tuple(
        _line(source, line_number=line_number)
        for line_number in range(1, first_count + 1)
    )
    second_lines = tuple(
        _line(source, line_number=line_number)
        for line_number in range(first_count + 1, _MAX_INDEXED_REFS + 2)
    )
    first_refs = tuple(
        _line_ref(batch_index=0, line_number=line_number)
        for line_number in range(1, first_count + 1)
    )
    second_refs = tuple(
        _line_ref(batch_index=1, line_number=line_number)
        for line_number in range(first_count + 1, _MAX_INDEXED_REFS + 2)
    )
    first_draft = _draft_batch(
        source,
        batch_index=0,
        lines=first_lines,
        indexed_refs=first_refs,
    )
    second_draft = _draft_batch(
        source,
        batch_index=1,
        lines=second_lines,
        indexed_refs=second_refs,
    )
    batches = tuple(
        draft.model_copy(
            update={
                "batch_fingerprint": _canonical_batch_fingerprint(
                    draft,
                    is_last=False,
                )
            }
        )
        for draft in (first_draft, second_draft)
    )
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=batches))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == batches[0]
        with pytest.raises(SecurityPolicyError) as raised:
            await anext(stream)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": _PARSER_ID,
        "limit": _MAX_INDEXED_REFS,
        "resource": "indexed_refs",
    }


@pytest.mark.anyio
async def test_legacy_schema_1_0_keeps_opaque_fingerprint_compatibility() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    batches = valid_extracted_batches(source)
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=batches))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        assert await anext(stream) == batches[0]
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    assert stream.completed is True


@pytest.mark.anyio
async def test_runtime_rejects_forged_unknown_schema_version() -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    canonical = valid_extracted_batches(source)[0]
    forged = canonical.model_copy(update={"schema_version": "1.2.0"})
    registry = ParserRegistry()
    registry.register(FakeParser(probe_result=_probe(source), batches=(forged,)))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        with pytest.raises(ParserError) as raised:
            await anext(stream)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "batch_schema"


def test_manifest_progressive_index_validation_is_bounded_and_fail_fast() -> None:
    source = _source()
    first_ref = _line_ref(batch_index=0, line_number=1)
    second_ref = _line_ref(batch_index=1, line_number=2)
    batches = _canonical_batches(
        source,
        lines_by_batch=(
            (_line(source, line_number=1),),
            (_line(source, line_number=2),),
        ),
        indexed_refs_by_batch=((first_ref,), (second_ref,)),
        manifest_refs=(second_ref, first_ref),
    )
    manifest = batches[-1].manifest
    assert manifest is not None
    consumed: list[int] = []

    def hostile_batches() -> Iterable[ExtractedBatch]:
        consumed.append(0)
        yield batches[0]
        consumed.append(1)
        raise AssertionError("hostile tail не должен быть прочитан")

    with pytest.raises(ValueError, match="progressive indexed refs"):
        manifest.validate_batches(hostile_batches())

    assert consumed == [0]
