"""Контракты physical text extraction, добавляемые milestone M4."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

import structuraguard.contracts.source as source_contracts
from structuraguard.contracts import (
    ExtensionMetadataEntry,
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedDatasetManifest,
    ExtractedLine,
    ExtractedSourceIndex,
    LineRangeLocation,
    PhysicalMetadataEntry,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    SourceArtifactRef,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
BATCH_FINGERPRINT = "sha256:" + "b" * 64
EXTRACTION_FINGERPRINT = "sha256:" + "c" * 64
OPTIONS_FINGERPRINT = "sha256:" + "d" * 64


def _source() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _producer(
    *,
    component_id: str = "parser.text",
    component_version: str = "1.0.0",
) -> ProducerMetadata:
    return ProducerMetadata(
        component_id=component_id,
        component_version=component_version,
        sdk_version="0.3.0",
    )


def _location(
    *,
    column_start: int | None = None,
    column_end: int | None = None,
) -> LineRangeLocation:
    return LineRangeLocation(
        source=_source(),
        line_start=1,
        line_end=1,
        column_start=column_start,
        column_end=column_end,
    )


def _line(
    *,
    line_id: str = "line-1",
    location: LineRangeLocation | None = None,
    metadata: tuple[PhysicalMetadataEntry, ...] = (),
) -> ExtractedLine:
    return ExtractedLine(
        line_id=line_id,
        line_number=1,
        text="raw value",
        location=location or _location(),
        metadata=metadata,
    )


def _line_ref(*, local_id: str = "line-1") -> PhysicalSourceRef:
    return PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=0,
        kind=PhysicalObjectKind.LINE,
        local_id=local_id,
    )


def _block(
    *,
    metadata: tuple[PhysicalMetadataEntry, ...] = (),
) -> ExtractedBlock:
    return ExtractedBlock(
        block_id="block-1",
        kind=ExtractedBlockKind.PARAGRAPH,
        order=0,
        location=_location(),
        text="raw value",
        metadata=metadata,
    )


def _batch(
    *,
    schema_version: str = "1.0.0",
    extraction_id: str = "extraction-1",
    line: ExtractedLine | None = None,
    block: ExtractedBlock | None = None,
    record_count: int | None = None,
    indexed_refs: tuple[PhysicalSourceRef, ...] = (),
    is_last: bool = False,
    manifest: ExtractedDatasetManifest | None = None,
) -> ExtractedBatch:
    return ExtractedBatch(
        schema_version=schema_version,
        extraction_id=extraction_id,
        batch_index=0,
        source=_source(),
        parser_id="parser.text",
        parser_version="1.0.0",
        batch_fingerprint=BATCH_FINGERPRINT,
        lines=((line or _line()),),
        blocks=((block,) if block is not None else ()),
        record_count=record_count,
        indexed_refs=indexed_refs,
        is_last=is_last,
        manifest=manifest,
    )


def _manifest(
    summary: ExtractedBatchSummary,
    *,
    schema_version: str = "1.0.0",
    record_count: int | None = None,
    source_index: ExtractedSourceIndex | None = None,
    producer: ProducerMetadata | None = None,
    parser_options_fingerprint: str | None = None,
) -> ExtractedDatasetManifest:
    return ExtractedDatasetManifest(
        schema_version=schema_version,
        source=_source(),
        extraction_id="extraction-1",
        parser_id="parser.text",
        parser_version="1.0.0",
        batches=(summary,),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=source_index or ExtractedSourceIndex(),
        record_count=record_count,
        producer=producer,
        parser_options_fingerprint=parser_options_fingerprint,
    )


def test_line_range_columns_are_optional_zero_based_and_end_exclusive() -> None:
    legacy = _location()
    empty_span = _location(column_start=0, column_end=0)
    non_empty_span = _location(column_start=2, column_end=5)

    assert (legacy.column_start, legacy.column_end) == (None, None)
    assert (empty_span.column_start, empty_span.column_end) == (0, 0)
    assert (non_empty_span.column_start, non_empty_span.column_end) == (2, 5)
    assert (
        LineRangeLocation.model_validate_json(non_empty_span.model_dump_json())
        == non_empty_span
    )


def test_multiline_span_allows_independent_end_column() -> None:
    location = LineRangeLocation(
        source=_source(),
        line_start=1,
        line_end=2,
        column_start=8,
        column_end=2,
    )

    assert (location.column_start, location.column_end) == (8, 2)


@pytest.mark.parametrize(
    ("column_start", "column_end"),
    (
        (0, None),
        (None, 0),
        (-1, 0),
        (0, -1),
        (5, 4),
    ),
)
def test_line_range_rejects_partial_negative_or_reversed_single_line_span(
    column_start: int | None,
    column_end: int | None,
) -> None:
    with pytest.raises(ValidationError):
        _location(column_start=column_start, column_end=column_end)


def test_physical_metadata_reuses_the_bounded_extension_entry_contract() -> None:
    assert PhysicalMetadataEntry is ExtensionMetadataEntry

    with pytest.raises(ValidationError):
        PhysicalMetadataEntry(
            key="line_ending",
            value="x" * 4_097,
        )


@pytest.mark.parametrize(
    "factory",
    (
        lambda metadata: _line(metadata=metadata),
        lambda metadata: _block(metadata=metadata),
    ),
)
def test_line_and_block_metadata_are_bounded_unique_key_tuples(
    factory: Callable[
        [tuple[PhysicalMetadataEntry, ...]],
        ExtractedLine | ExtractedBlock,
    ],
) -> None:
    metadata = (
        PhysicalMetadataEntry(key="line_ending", value="lf"),
        PhysicalMetadataEntry(key="syntax_kind", value="paragraph"),
    )
    created = factory(metadata)
    assert created.metadata == metadata

    duplicate = (
        PhysicalMetadataEntry(key="line_ending", value="lf"),
        PhysicalMetadataEntry(key="line_ending", value="crlf"),
    )
    with pytest.raises(ValidationError):
        factory(duplicate)

    maximum = tuple(
        PhysicalMetadataEntry(key=f"metadata-{index}", value=index)
        for index in range(64)
    )
    assert len(factory(maximum).metadata) == 64
    with pytest.raises(ValidationError):
        factory(
            (
                *maximum,
                PhysicalMetadataEntry(key="metadata-overflow", value=True),
            )
        )


def test_legacy_extracted_contract_defaults_remain_accepted() -> None:
    batch = _batch()
    summary = batch.to_summary()
    manifest = _manifest(summary)

    assert batch.schema_version == "1.0.0"
    assert batch.record_count is None
    assert batch.indexed_refs == ()
    assert batch.lines[0].metadata == ()
    assert _block().metadata == ()
    assert summary.record_count is None
    assert manifest.schema_version == "1.0.0"
    assert manifest.record_count is None
    assert manifest.producer is None
    assert manifest.parser_options_fingerprint is None
    assert ExtractedBatch.model_validate_json(batch.model_dump_json()) == batch
    assert (
        ExtractedDatasetManifest.model_validate_json(manifest.model_dump_json())
        == manifest
    )

    batch_payload = json.loads(batch.canonical_json())
    summary_payload = json.loads(summary.canonical_json())
    manifest_payload = json.loads(manifest.canonical_json())
    assert "record_count" not in batch_payload
    assert "indexed_refs" not in batch_payload
    assert "metadata" not in batch_payload["lines"][0]
    assert "record_count" not in summary_payload
    assert "record_count" not in manifest_payload
    assert "producer" not in manifest_payload
    assert "parser_options_fingerprint" not in manifest_payload


@pytest.mark.parametrize(
    "batch_factory",
    (
        lambda: _batch(line=_line(location=_location(column_start=0, column_end=9))),
        lambda: _batch(
            line=_line(metadata=(PhysicalMetadataEntry(key="line_ending", value="lf"),))
        ),
        lambda: _batch(
            block=_block(
                metadata=(PhysicalMetadataEntry(key="syntax_kind", value="paragraph"),)
            )
        ),
        lambda: _batch(record_count=1),
        lambda: _batch(indexed_refs=(_line_ref(),)),
    ),
)
def test_legacy_schema_rejects_non_default_m04_fields(
    batch_factory: Callable[[], ExtractedBatch],
) -> None:
    with pytest.raises(ValidationError):
        batch_factory()


def test_schema_1_1_batch_carries_counts_metadata_and_current_index_refs() -> None:
    metadata = (PhysicalMetadataEntry(key="line_ending", value="none"),)
    line = _line(
        location=_location(column_start=0, column_end=9),
        metadata=metadata,
    )
    batch = _batch(
        schema_version="1.1.0",
        line=line,
        record_count=1,
        indexed_refs=(_line_ref(),),
    )
    summary = batch.to_summary()

    assert batch.record_count == 1
    assert batch.indexed_refs == (_line_ref(),)
    assert batch.lines[0].metadata == metadata
    assert summary.record_count == 1
    payload = json.loads(batch.canonical_json())
    assert payload["record_count"] == 1
    assert payload["indexed_refs"][0]["local_id"] == "line-1"
    assert payload["lines"][0]["metadata"][0]["key"] == "line_ending"


def test_internal_batch_validation_does_not_materialize_public_physical_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexed_ref = _line_ref()
    draft = _batch(
        schema_version="1.1.0",
        record_count=1,
        indexed_refs=(indexed_ref,),
    )
    manifest = _manifest(
        draft.to_summary(),
        schema_version="1.1.0",
        record_count=1,
        source_index=ExtractedSourceIndex(refs=(indexed_ref,)),
        producer=_producer(),
        parser_options_fingerprint=OPTIONS_FINGERPRINT,
    )
    original_ref_type = PhysicalSourceRef
    constructed_refs = 0

    def counted_ref(
        *,
        extraction_id: str,
        batch_index: int,
        kind: PhysicalObjectKind,
        local_id: str,
    ) -> PhysicalSourceRef:
        nonlocal constructed_refs
        constructed_refs += 1
        return original_ref_type(
            extraction_id=extraction_id,
            batch_index=batch_index,
            kind=kind,
            local_id=local_id,
        )

    monkeypatch.setattr(source_contracts, "PhysicalSourceRef", counted_ref)

    terminal = _batch(
        schema_version="1.1.0",
        record_count=1,
        indexed_refs=(indexed_ref,),
        is_last=True,
        manifest=manifest,
    )
    assert terminal.to_summary().physical_ref_count == 1
    assert constructed_refs == 0

    assert terminal.physical_refs() == (indexed_ref,)
    assert constructed_refs == 1


@pytest.mark.parametrize(
    ("extraction_id", "line_id", "expected_message"),
    (
        (
            "run-1",
            "line-1",
            "extraction_id не является parser-generated identifier",
        ),
        (
            "extraction-1",
            "block-1",
            "local_id не соответствует physical object kind",
        ),
    ),
)
def test_batch_rejects_invalid_unindexed_generated_physical_ids(
    extraction_id: str,
    line_id: str,
    expected_message: str,
) -> None:
    with pytest.raises(ValidationError, match=expected_message):
        _batch(
            schema_version="1.1.0",
            extraction_id=extraction_id,
            line=_line(line_id=line_id),
            record_count=1,
        )


@pytest.mark.parametrize("record_count", (-1, True, 1.0))
def test_record_count_requires_an_exact_non_negative_integer(
    record_count: object,
) -> None:
    with pytest.raises(ValidationError):
        _batch(
            schema_version="1.1.0",
            record_count=record_count,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "indexed_refs",
    (
        (_line_ref(local_id="line-2"),),
        (_line_ref(), _line_ref()),
        (
            PhysicalSourceRef(
                extraction_id="extraction-1",
                batch_index=1,
                kind=PhysicalObjectKind.LINE,
                local_id="line-1",
            ),
        ),
    ),
)
def test_schema_1_1_batch_rejects_unknown_duplicate_or_foreign_index_refs(
    indexed_refs: tuple[PhysicalSourceRef, ...],
) -> None:
    with pytest.raises(ValidationError):
        _batch(
            schema_version="1.1.0",
            record_count=1,
            indexed_refs=indexed_refs,
        )


def test_schema_1_1_requires_record_count_and_manifest_aggregate() -> None:
    with pytest.raises(ValidationError):
        _batch(schema_version="1.1.0")

    draft = _batch(schema_version="1.1.0", record_count=1)
    summary = draft.to_summary()

    with pytest.raises(ValidationError):
        _manifest(
            summary,
            schema_version="1.1.0",
            producer=_producer(),
            parser_options_fingerprint=OPTIONS_FINGERPRINT,
        )
    with pytest.raises(ValidationError):
        _manifest(
            summary,
            schema_version="1.1.0",
            record_count=2,
            producer=_producer(),
            parser_options_fingerprint=OPTIONS_FINGERPRINT,
        )

    manifest = _manifest(
        summary,
        schema_version="1.1.0",
        record_count=1,
        producer=_producer(),
        parser_options_fingerprint=OPTIONS_FINGERPRINT,
    )
    terminal = _batch(
        schema_version="1.1.0",
        record_count=1,
        is_last=True,
        manifest=manifest,
    )

    assert terminal.manifest == manifest
    assert terminal.manifest.record_count == 1
    assert terminal.manifest.producer == _producer()
    assert terminal.manifest.parser_options_fingerprint == OPTIONS_FINGERPRINT
    manifest_payload = json.loads(terminal.manifest.canonical_json())
    assert manifest_payload["producer"]["component_id"] == "parser.text"
    assert manifest_payload["parser_options_fingerprint"] == OPTIONS_FINGERPRINT


@pytest.mark.parametrize(
    ("producer", "parser_options_fingerprint"),
    (
        (None, OPTIONS_FINGERPRINT),
        (_producer(), None),
    ),
)
def test_schema_1_1_manifest_requires_producer_and_parser_options_fingerprint(
    producer: ProducerMetadata | None,
    parser_options_fingerprint: str | None,
) -> None:
    summary = _batch(schema_version="1.1.0", record_count=1).to_summary()

    with pytest.raises(
        ValidationError,
        match=r"producer.*parser_options_fingerprint",
    ):
        _manifest(
            summary,
            schema_version="1.1.0",
            record_count=1,
            producer=producer,
            parser_options_fingerprint=parser_options_fingerprint,
        )


@pytest.mark.parametrize(
    "producer",
    (
        _producer(component_id="parser.other"),
        _producer(component_version="2.0.0"),
    ),
)
def test_schema_1_1_manifest_binds_producer_identity_to_parser(
    producer: ProducerMetadata,
) -> None:
    summary = _batch(schema_version="1.1.0", record_count=1).to_summary()

    with pytest.raises(ValidationError, match="Producer identity"):
        _manifest(
            summary,
            schema_version="1.1.0",
            record_count=1,
            producer=producer,
            parser_options_fingerprint=OPTIONS_FINGERPRINT,
        )


@pytest.mark.parametrize(
    ("producer", "parser_options_fingerprint"),
    (
        (_producer(), None),
        (None, OPTIONS_FINGERPRINT),
    ),
)
def test_legacy_manifest_rejects_m04_lineage_fields(
    producer: ProducerMetadata | None,
    parser_options_fingerprint: str | None,
) -> None:
    summary = _batch().to_summary()

    with pytest.raises(ValidationError, match=r"Schema 1\.0\.0"):
        _manifest(
            summary,
            producer=producer,
            parser_options_fingerprint=parser_options_fingerprint,
        )


def test_terminal_batch_and_manifest_schema_versions_must_match() -> None:
    draft = _batch(schema_version="1.1.0", record_count=1)
    manifest = _manifest(
        draft.to_summary(),
        schema_version="1.1.0",
        record_count=1,
        producer=_producer(),
        parser_options_fingerprint=OPTIONS_FINGERPRINT,
    )

    with pytest.raises(ValidationError):
        _batch(
            schema_version="1.0.0",
            is_last=True,
            manifest=manifest,
        )


@pytest.mark.parametrize("schema_version", ("0.9.0", "1.2.0", "2.0.0"))
def test_physical_extraction_rejects_unknown_schema_versions(
    schema_version: str,
) -> None:
    with pytest.raises(ValidationError, match="Неподдерживаемая версия"):
        _batch(schema_version=schema_version)

    legacy_summary = _batch().to_summary()
    with pytest.raises(ValidationError, match="Неподдерживаемая версия"):
        _manifest(legacy_summary, schema_version=schema_version)


def test_manifest_methods_reject_model_copy_with_unknown_schema_version() -> None:
    batch = _batch()
    manifest = _manifest(batch.to_summary()).model_copy(
        update={"schema_version": "1.2.0"}
    )

    with pytest.raises(ValueError, match="Неподдерживаемая версия"):
        manifest.validate_batches((batch,))
