from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    BatchFingerprint,
    CatalogColumnRef,
    ColumnCatalog,
    CssSelectorLocation,
    DatabaseCatalog,
    DecimalScalar,
    ExtensionMetadataEntry,
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedDatasetManifest,
    ExtractedLine,
    ExtractedSourceIndex,
    FieldMapping,
    IntegerScalar,
    JsonPointerLocation,
    LineRangeLocation,
    LoadOperation,
    MappingPlan,
    MappingPlanValidationRequest,
    MappingPolicyRef,
    NormalizedBatch,
    NormalizedDatasetManifest,
    NormalizedRecord,
    NormalizedValue,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    SchemaCatalog,
    SemanticEntity,
    SemanticField,
    SemanticFieldRef,
    SemanticSourceIndex,
    SourceArtifactRef,
    StringScalar,
    TableCatalog,
    XPathLocation,
)
from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.normalized import NormalizedBatchSummary
from structuraguard.contracts.source import ExtractedBatchSummary
from structuraguard.ports.source import ParseContext, ProbeContext

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
EXTRACTION_FINGERPRINT = "sha256:" + "b" * 64
PARSE_PLAN_FINGERPRINT = "sha256:" + "c" * 64
NORMALIZED_FINGERPRINT = "sha256:" + "d" * 64
DATABASE_FINGERPRINT = "sha256:" + "e" * 64
POLICY_FINGERPRINT = "sha256:" + "f" * 64


def _source() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _producer() -> ProducerMetadata:
    return ProducerMetadata(
        component_id="tests.executor",
        component_version="1.0.0",
        sdk_version="0.2.0",
    )


def _physical_ref(batch_index: int) -> PhysicalSourceRef:
    return PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=batch_index,
        kind=PhysicalObjectKind.LINE,
        local_id=f"line-{batch_index}",
    )


def _extracted_batch(
    batch_index: int,
    *,
    extraction_id: str = "extraction-1",
    is_last: bool = False,
    manifest: ExtractedDatasetManifest | None = None,
) -> ExtractedBatch:
    location = LineRangeLocation(
        source=_source(),
        line_start=batch_index + 1,
        line_end=batch_index + 1,
    )
    return ExtractedBatch(
        extraction_id=extraction_id,
        batch_index=batch_index,
        source=_source(),
        parser_id="parser.csv",
        parser_version="1.0.0",
        batch_fingerprint="sha256:" + str(batch_index + 1) * 64,
        lines=(
            ExtractedLine(
                line_id=f"line-{batch_index}",
                line_number=batch_index + 1,
                text=f"row-{batch_index}",
                location=location,
            ),
        ),
        is_last=is_last,
        manifest=manifest,
    )


def test_extracted_manifest_rejects_foreign_batch_summary_and_sequence() -> None:
    first = _extracted_batch(0)
    terminal_seed = _extracted_batch(1)

    foreign_summary = terminal_seed.to_summary().model_copy(
        update={"extraction_id": "extraction-2"}
    )
    with pytest.raises(ValidationError, match="другому extraction"):
        ExtractedDatasetManifest(
            source=_source(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batches=(first.to_summary(), foreign_summary),
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            source_index=ExtractedSourceIndex(
                refs=(_physical_ref(0), _physical_ref(1))
            ),
        )

    manifest = ExtractedDatasetManifest(
        source=_source(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batches=(first.to_summary(), terminal_seed.to_summary()),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(refs=(_physical_ref(0),)),
    )
    terminal = _extracted_batch(1, is_last=True, manifest=manifest)
    manifest.validate_batches((first, terminal))

    foreign_first = _extracted_batch(0, extraction_id="extraction-2")
    with pytest.raises(ValueError, match="summary"):
        manifest.validate_batches((foreign_first, terminal))


def test_extracted_source_index_is_bounded_selective_allowlist() -> None:
    refs = tuple(
        PhysicalSourceRef(
            extraction_id="extraction-1",
            batch_index=0,
            kind=PhysicalObjectKind.VALUE,
            local_id=f"value-{index}",
        )
        for index in range(10_001)
    )
    with pytest.raises(ValidationError):
        ExtractedSourceIndex(refs=refs)


def _record(batch_index: int, *, record_id: str) -> NormalizedRecord:
    source_ref = _physical_ref(batch_index)
    value = NormalizedValue(
        value_id=f"value-{batch_index}",
        field_name="total_amount",
        raw_value=StringScalar(value=str(batch_index)),
        normalized_value=DecimalScalar(value=Decimal(batch_index)),
        semantic_type="money",
        source_refs=(source_ref,),
        transformations=("parse_decimal",),
    )
    entity = SemanticEntity(
        entity_id=f"entity-{batch_index}",
        entity_type="order",
        values=(value,),
        source_refs=(source_ref,),
    )
    return NormalizedRecord(
        record_id=record_id,
        entities=(entity,),
        source_refs=(source_ref,),
    )


def _normalized_batch(
    batch_index: int,
    *,
    record_id: str,
    is_last: bool = False,
    manifest: NormalizedDatasetManifest | None = None,
) -> NormalizedBatch:
    return NormalizedBatch(
        source=_source(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer(),
        batch_index=batch_index,
        batch_fingerprint="sha256:" + str(batch_index + 3) * 64,
        records=(_record(batch_index, record_id=record_id),),
        is_last=is_last,
        manifest=manifest,
    )


def _normalized_manifest(
    summaries: tuple[NormalizedBatchSummary, ...],
    *,
    record_count: int = 2,
) -> NormalizedDatasetManifest:
    field = SemanticField(
        entity_type="order",
        field_name="total_amount",
        semantic_type="money",
    )
    field_ref = SemanticFieldRef(
        entity_type=field.entity_type,
        field_name=field.field_name,
    )
    return NormalizedDatasetManifest(
        source=_source(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer(),
        batches=summaries,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        record_count=record_count,
        entity_count=sum(summary.entity_count for summary in summaries),
        value_count=sum(summary.value_count for summary in summaries),
        semantic_fields=(field.field_name,),
        semantic_index=SemanticSourceIndex(fields=(field_ref,)),
        semantic_schema=(field,),
    )


def test_normalized_stream_rejects_global_duplicate_ids_and_count_mismatch() -> None:
    first = _normalized_batch(0, record_id="record-duplicate")
    terminal_seed = _normalized_batch(1, record_id="record-duplicate")
    summaries = (first.to_summary(), terminal_seed.to_summary())

    with pytest.raises(ValidationError, match="record_count"):
        _normalized_manifest(summaries, record_count=3)

    manifest = _normalized_manifest(summaries)
    terminal = _normalized_batch(
        1,
        record_id="record-duplicate",
        is_last=True,
        manifest=manifest,
    )
    with pytest.raises(ValueError, match="record_id"):
        manifest.validate_batches((first, terminal))


def test_single_batch_manifests_require_lineage_aware_cardinality_summaries() -> None:
    fingerprint_only = BatchFingerprint(
        batch_index=0,
        batch_fingerprint="sha256:" + "3" * 64,
    )

    with pytest.raises(ValidationError):
        ExtractedDatasetManifest.model_validate(
            {
                "source": _source(),
                "extraction_id": "extraction-1",
                "parser_id": "parser.csv",
                "parser_version": "1.0.0",
                "batches": (fingerprint_only,),
                "extraction_fingerprint": EXTRACTION_FINGERPRINT,
                "source_index": ExtractedSourceIndex(),
            }
        )

    with pytest.raises(ValidationError):
        NormalizedDatasetManifest.model_validate(
            {
                "source": _source(),
                "extraction_id": "extraction-1",
                "extraction_fingerprint": EXTRACTION_FINGERPRINT,
                "parse_plan_fingerprint": PARSE_PLAN_FINGERPRINT,
                "producer": _producer(),
                "batches": (fingerprint_only,),
                "normalized_fingerprint": NORMALIZED_FINGERPRINT,
            }
        )


def test_changed_normalized_value_requires_transformation_evidence() -> None:
    with pytest.raises(ValidationError, match="transformation"):
        NormalizedValue(
            value_id="value-1",
            field_name="quantity",
            raw_value=StringScalar(value="100"),
            normalized_value=IntegerScalar(value=100),
            semantic_type="integer",
            source_refs=(_physical_ref(0),),
        )

    unchanged = NormalizedValue(
        value_id="value-2",
        field_name="label",
        raw_value=StringScalar(value="untouched"),
        normalized_value=StringScalar(value="untouched"),
        semantic_type="text",
        source_refs=(_physical_ref(0),),
    )
    assert unchanged.transformations == ()


def _catalog() -> DatabaseCatalog:
    column = ColumnCatalog(
        column_id="orders.total_amount",
        name="total_amount",
        type_name="numeric",
        nullable=False,
    )
    table = TableCatalog(
        table_id="public.orders",
        schema_name="public",
        name="orders",
        columns=(column,),
    )
    return DatabaseCatalog(
        dialect="postgresql",
        target_id="main",
        database_fingerprint=DATABASE_FINGERPRINT,
        target_policy_fingerprint=POLICY_FINGERPRINT,
        producer=_producer(),
        schemas=(SchemaCatalog(schema_id="public", name="public", tables=(table,)),),
    )


def _mapping_plan_payload() -> dict[str, object]:
    field_ref = SemanticFieldRef(entity_type="order", field_name="total_amount")
    return {
        "plan_id": "mapping-plan-1",
        "schema_version": "1.0.0",
        "revision": 1,
        "fingerprint": "sha256:" + "0" * 64,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "extraction_fingerprint": EXTRACTION_FINGERPRINT,
        "parse_plan_fingerprint": PARSE_PLAN_FINGERPRINT,
        "normalized_fingerprint": NORMALIZED_FINGERPRINT,
        "database_fingerprint": DATABASE_FINGERPRINT,
        "target_id": "main",
        "target_policy_fingerprint": POLICY_FINGERPRINT,
        "mappings": (
            FieldMapping(
                source=field_ref,
                target=CatalogColumnRef(
                    table_id="public.orders",
                    column_id="orders.total_amount",
                ),
            ),
        ),
        "operation": LoadOperation.INSERT_ONLY,
        "confidence": Decimal("1"),
        "producer": _producer(),
    }


def _mapping_plan() -> MappingPlan:
    payload = _mapping_plan_payload()
    payload["fingerprint"] = canonical_sha256_value(
        cast(CanonicalInput, payload),
        exclude_top_level=frozenset({"fingerprint"}),
    )
    return MappingPlan.model_validate(payload)


def test_mapping_plan_rejects_stale_fingerprint_after_content_change() -> None:
    plan = _mapping_plan()
    assert plan.fingerprint == plan.content_fingerprint()

    payload_without_fingerprint = _mapping_plan_payload()
    payload_without_fingerprint.pop("fingerprint")
    derived = MappingPlan.model_validate(payload_without_fingerprint)
    assert derived.fingerprint == derived.content_fingerprint()

    payload = plan.model_dump(mode="python")
    payload["revision"] = 2
    with pytest.raises(ValidationError, match="fingerprint"):
        MappingPlan.model_validate(payload)


def test_mapping_validation_requires_typed_semantic_schema() -> None:
    seed = _normalized_batch(0, record_id="record-1")
    summary = seed.to_summary()
    field_ref = SemanticFieldRef(entity_type="order", field_name="total_amount")
    with pytest.raises(ValidationError, match="semantic schema"):
        NormalizedDatasetManifest(
            source=_source(),
            extraction_id="extraction-1",
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            producer=_producer(),
            batches=(summary,),
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            record_count=1,
            entity_count=1,
            value_count=1,
            semantic_fields=("total_amount",),
            semantic_index=SemanticSourceIndex(fields=(field_ref,)),
        )

    manifest = _normalized_manifest((summary,), record_count=1)
    request = MappingPlanValidationRequest(
        plan=_mapping_plan(),
        manifest=manifest,
        catalog=_catalog(),
        policy=MappingPolicyRef(
            policy_id="mapping-policy-1",
            policy_fingerprint=POLICY_FINGERPRINT,
        ),
    )
    assert request.manifest.semantic_schema[0].semantic_type == "money"


@pytest.mark.parametrize(
    ("constructor", "field_name", "value"),
    (
        (ExtractedLine, "text", b"raw-bytes"),
        (JsonPointerLocation, "pointer", b"/field"),
        (XPathLocation, "xpath", b"/root/field"),
        (CssSelectorLocation, "selector", b"table > td"),
    ),
)
def test_physical_text_fields_reject_implicit_bytes_coercion(
    constructor: (
        type[ExtractedLine]
        | type[JsonPointerLocation]
        | type[XPathLocation]
        | type[CssSelectorLocation]
    ),
    field_name: str,
    value: bytes,
) -> None:
    if constructor is ExtractedLine:
        kwargs: dict[str, object] = {
            "line_id": "line-1",
            "line_number": 1,
            "text": value,
            "location": LineRangeLocation(
                source=_source(),
                line_start=1,
                line_end=1,
            ),
        }
    else:
        kwargs = {"source": _source(), field_name: value}
    with pytest.raises(ValidationError):
        constructor.model_validate(kwargs)


def test_extracted_block_rejects_implicit_bytes_coercion() -> None:
    with pytest.raises(ValidationError):
        ExtractedBlock.model_validate(
            {
                "block_id": "block-1",
                "kind": ExtractedBlockKind.PARAGRAPH,
                "order": 0,
                "location": LineRangeLocation(
                    source=_source(),
                    line_start=1,
                    line_end=1,
                ),
                "text": b"raw-bytes",
            }
        )


def test_extension_metadata_rejects_decimal_to_float_coercion() -> None:
    with pytest.raises(ValidationError):
        ExtensionMetadataEntry.model_validate({"key": "score", "value": Decimal("1.1")})


@dataclass(frozen=True)
class _Reader:
    label: str
    source_fingerprint: str = SOURCE_FINGERPRINT

    async def read(self, *, offset: int, size: int) -> bytes:
        return b""[offset : offset + size]


@pytest.mark.parametrize(
    "context_factory",
    (
        lambda reader: ProbeContext(
            reader=reader,
            source_fingerprint=SOURCE_FINGERPRINT,
            max_probe_bytes=128,
        ),
        lambda reader: ParseContext(
            reader=reader,
            source_fingerprint=SOURCE_FINGERPRINT,
            max_bytes=1024,
            max_records=10,
            max_nesting_depth=4,
        ),
    ),
)
def test_live_reader_is_excluded_from_context_equality_and_repr(
    context_factory: Callable[[_Reader], ProbeContext | ParseContext],
) -> None:
    first = context_factory(_Reader(label="secret-dsn-one"))
    second = context_factory(_Reader(label="secret-dsn-two"))
    assert first == second
    assert "secret-dsn" not in repr(first)


def test_summary_types_are_frozen_serializable_contracts() -> None:
    extracted_summary = _extracted_batch(0).to_summary()
    normalized_summary = _normalized_batch(0, record_id="record-1").to_summary()

    assert (
        ExtractedBatchSummary.model_validate_json(extracted_summary.model_dump_json())
        == extracted_summary
    )
    assert (
        NormalizedBatchSummary.model_validate_json(normalized_summary.model_dump_json())
        == normalized_summary
    )
    assert isinstance(extracted_summary, BatchFingerprint)
    assert isinstance(normalized_summary, BatchFingerprint)
