from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from structuraguard.contracts import (
    ColumnCatalog,
    DatabaseCatalog,
    DataClassification,
    DecimalScalar,
    DocumentShapeObservation,
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ExtractedTable,
    IntegerScalar,
    LineRangeLocation,
    MappingPlan,
    NormalizedBatch,
    NormalizedBatchSummary,
    NormalizedDatasetManifest,
    NormalizedRecord,
    NormalizedValue,
    ParseEntity,
    ParseEntityGrouping,
    ParseField,
    ParseFieldSelector,
    PhysicalObjectKind,
    PhysicalSample,
    PhysicalSourceRef,
    ProducerMetadata,
    RecordRangeRule,
    SchemaCatalog,
    SemanticEntity,
    SemanticField,
    SemanticFieldRef,
    SemanticSourceIndex,
    SourceArtifact,
    SourceArtifactRef,
    StringScalar,
    StructureEvidence,
    StructureProfile,
    TableCatalog,
    TabularColumnSelector,
    TabularParsePlan,
    TabularRowGrouping,
    TabularShapeObservation,
)
from structuraguard.contracts._base import CanonicalInput
from structuraguard.domain import canonical_json, sha256_fingerprint

if TYPE_CHECKING:
    from structuraguard.contracts import SecurityApproval

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
EXTRACTION_FINGERPRINT = "sha256:" + "b" * 64
PROFILE_FINGERPRINT = "sha256:" + "c" * 64
PARSE_PLAN_FINGERPRINT = (
    "sha256:9abc955f621d451aee8aa349cc70830f932ca744782583bc989640e2f5bc6b4e"
)
NORMALIZED_FINGERPRINT = "sha256:" + "e" * 64
DATABASE_FINGERPRINT = "sha256:" + "f" * 64
POLICY_FINGERPRINT = "sha256:" + "1" * 64
BATCH_FINGERPRINT = "sha256:" + "2" * 64
MAPPING_PLAN_FINGERPRINT = (
    "sha256:32fb79df886fb41ad6fbf758eecfb3534a0825c7b0c808f85aa65c3506b9a5dc"
)
MAPPING_VALIDATION_FINGERPRINT = "sha256:" + "6" * 64
AUDIT_EVENT_ID = "event-550e8400-e29b-41d4-a716-446655440000"
AUDIT_RUN_ID = "run-123e4567-e89b-42d3-a456-426614174000"

LOAD_ARTIFACT_FINGERPRINTS = (
    SOURCE_FINGERPRINT,
    EXTRACTION_FINGERPRINT,
    PARSE_PLAN_FINGERPRINT,
    NORMALIZED_FINGERPRINT,
    MAPPING_PLAN_FINGERPRINT,
    MAPPING_VALIDATION_FINGERPRINT,
    DATABASE_FINGERPRINT,
    POLICY_FINGERPRINT,
)


def _producer(component_id: str = "tests.contracts") -> ProducerMetadata:
    return ProducerMetadata(
        component_id=component_id,
        component_version="1.0.0",
        sdk_version="0.2.0",
    )


def _source_ref() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _extracted_summary(
    *,
    batch_index: int = 0,
    batch_fingerprint: str = BATCH_FINGERPRINT,
    physical_ref_count: int = 0,
) -> ExtractedBatchSummary:
    return ExtractedBatchSummary(
        batch_index=batch_index,
        batch_fingerprint=batch_fingerprint,
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        physical_ref_count=physical_ref_count,
    )


def _normalized_summary(
    *,
    batch_index: int = 0,
    batch_fingerprint: str = BATCH_FINGERPRINT,
    record_count: int = 0,
    entity_count: int = 0,
    value_count: int = 0,
) -> NormalizedBatchSummary:
    return NormalizedBatchSummary(
        batch_index=batch_index,
        batch_fingerprint=batch_fingerprint,
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        record_count=record_count,
        entity_count=entity_count,
        value_count=value_count,
    )


def _physical_ref(
    *,
    kind: PhysicalObjectKind = PhysicalObjectKind.BLOCK,
    local_id: str = "block-1",
) -> PhysicalSourceRef:
    return PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=0,
        kind=kind,
        local_id=local_id,
    )


def _structure_evidence(reference: PhysicalSourceRef) -> StructureEvidence:
    if reference.kind is PhysicalObjectKind.TABLE:
        return StructureEvidence(
            evidence_id=f"evidence-{reference.local_id}",
            code="physical_shape_observed",
            source_refs=(reference,),
            observation=TabularShapeObservation(
                table_ref=reference,
                sampled_row_count=1,
                column_count=1,
                header_candidates=(0,),
            ),
        )
    return StructureEvidence(
        evidence_id=f"evidence-{reference.local_id}",
        code="physical_shape_observed",
        source_refs=(reference,),
        observation=DocumentShapeObservation(block_refs=(reference,)),
    )


def _physical_sample(
    reference: PhysicalSourceRef | None = None,
) -> PhysicalSample:
    source_ref = reference or _physical_ref()
    return PhysicalSample(
        source_ref=source_ref,
        batch_fingerprint=BATCH_FINGERPRINT,
        raw_value=StringScalar(value="sample"),
        location=LineRangeLocation(
            source=_source_ref(),
            line_start=1,
            line_end=1,
        ),
    )


def _extracted_manifest() -> ExtractedDatasetManifest:
    return ExtractedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batches=(_extracted_summary(physical_ref_count=2),),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(
            refs=(
                _physical_ref(),
                _physical_ref(kind=PhysicalObjectKind.TABLE, local_id="table-1"),
            )
        ),
    )


def _parse_plan() -> TabularParsePlan:
    table_ref = _physical_ref(kind=PhysicalObjectKind.TABLE, local_id="table-1")
    field = ParseField(
        field_id="total_amount",
        semantic_name="total_amount",
        semantic_type="money",
        source_refs=(table_ref,),
        selector=TabularColumnSelector(column_index=0),
        rules=(RecordRangeRule(start_index=0, end_index=10),),
    )
    return TabularParsePlan(
        plan_id="parse-plan-1",
        schema_version="1.0.0",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        confidence=Decimal("0.95"),
        producer=_producer("deterministic.structure-analyzer"),
        fields=(field,),
        entities=(
            ParseEntity(
                entity_id="order",
                entity_type="order",
                field_ids=(field.field_id,),
                grouping=TabularRowGrouping(),
            ),
        ),
        rules=(),
        evidence=(table_ref,),
        table_ref=table_ref,
        header_row=0,
        data_start_row=1,
    )


def _rehash_parse_plan(
    plan: TabularParsePlan,
    **updates: object,
) -> TabularParsePlan:
    payload = plan.model_dump(mode="python")
    payload.pop("fingerprint")
    payload.update(updates)
    return TabularParsePlan.model_validate(payload)


def _normalized_manifest() -> NormalizedDatasetManifest:
    field_ref = SemanticFieldRef(
        entity_type="order",
        field_name="total_amount",
    )
    return NormalizedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batches=(_normalized_summary(),),
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        semantic_fields=("total_amount",),
        semantic_index=SemanticSourceIndex(fields=(field_ref,)),
        semantic_schema=(
            SemanticField(
                entity_type="order",
                field_name="total_amount",
                semantic_type="money",
            ),
        ),
    )


def _database_catalog() -> DatabaseCatalog:
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
        producer=_producer("database-inspector"),
        schemas=(SchemaCatalog(schema_id="public", name="public", tables=(table,)),),
    )


def _mapping_plan() -> MappingPlan:
    from structuraguard.contracts import CatalogColumnRef, FieldMapping, LoadOperation

    return MappingPlan(
        plan_id="mapping-plan-1",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        mappings=(
            FieldMapping(
                source=SemanticFieldRef(
                    entity_type="order",
                    field_name="total_amount",
                ),
                target=CatalogColumnRef(
                    table_id="public.orders",
                    column_id="orders.total_amount",
                ),
            ),
        ),
        operation=LoadOperation.INSERT_ONLY,
        confidence=Decimal("1"),
        producer=_producer("deterministic.mapper"),
    )


def _rehash_mapping_plan(
    plan: MappingPlan,
    **updates: object,
) -> MappingPlan:
    payload = plan.model_dump(mode="python")
    payload.pop("fingerprint")
    payload.update(updates)
    return MappingPlan.model_validate(payload)


def _security_approval(
    payload_json: str,
    *,
    run_id: str = "run-1",
) -> SecurityApproval:
    from structuraguard.contracts import (
        PipelineStatus,
        SecurityApproval,
        SecurityReport,
    )

    payload_fingerprint = (
        "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    )
    report = SecurityReport(
        request_id="security-request-1",
        run_id=run_id,
        purpose="llm_input",
        content_fingerprint=SOURCE_FINGERPRINT,
        payload_fingerprint=payload_fingerprint,
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="policy-1",
        routing_policy_fingerprint="4" * 64,
        redaction_fingerprint="1" * 64,
        producer=_producer("security-scanner"),
        decision="allowed",
        status=PipelineStatus.COMPLETED,
        artifact_fingerprints=(SOURCE_FINGERPRINT, payload_fingerprint),
        scanned_items=1,
        generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )
    return SecurityApproval(
        report=report,
        report_fingerprint=sha256_fingerprint(report),
    )


def test_canonical_serialization_and_fingerprint_are_repeatable() -> None:
    source = SourceArtifact(
        artifact_id="source-1",
        display_name="orders.csv",
        media_type="text/csv",
        size_bytes=128,
        source_fingerprint=SOURCE_FINGERPRINT,
    )

    first = canonical_json(source)
    second = canonical_json(
        SourceArtifact.model_validate_json(source.model_dump_json())
    )

    assert first == second
    assert first == source.canonical_json()
    assert first == (
        '{"artifact_id":"source-1","display_name":"orders.csv",'
        '"media_type":"text/csv","schema_version":"1.0.0",'
        '"size_bytes":128,"source_fingerprint":'
        '"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
    )
    assert sha256_fingerprint(source) == (
        "sha256:9cf02b17c102805b16606ced054f88d357bdeb5e04bbebd900b4eda89562372d"
    )
    assert sha256_fingerprint(source) != sha256_fingerprint(
        source.model_copy(update={"size_bytes": 129})
    )


def test_canonical_serialization_is_independent_of_mapping_order() -> None:
    first = {"z": 1, "nested": {"b": 2, "a": 1}}
    second = {"nested": {"a": 1, "b": 2}, "z": 1}

    first_input = cast(CanonicalInput, first)
    second_input = cast(CanonicalInput, second)
    assert canonical_json(first_input) == canonical_json(second_input)
    assert sha256_fingerprint(first_input) == sha256_fingerprint(second_input)


def test_rich_normalized_literal_has_lossless_stable_json_round_trip() -> None:
    physical_ref = {
        "extraction_id": "extraction-1",
        "batch_index": 0,
        "kind": "block",
        "local_id": "block-1",
    }
    expected_wire = {
        "schema_version": "1.0.0",
        "source": {
            "artifact_id": "source-1",
            "source_fingerprint": SOURCE_FINGERPRINT,
        },
        "extraction_id": "extraction-1",
        "extraction_fingerprint": EXTRACTION_FINGERPRINT,
        "parse_plan_fingerprint": PARSE_PLAN_FINGERPRINT,
        "producer": {
            "component_id": "parse-plan-executor",
            "component_version": "1.0.0",
            "sdk_version": "0.2.0",
            "provider_id": None,
            "model_id": None,
            "prompt_fingerprint": None,
            "generation_version": None,
        },
        "batch_index": 0,
        "batch_fingerprint": BATCH_FINGERPRINT,
        "records": [
            {
                "record_id": "record-1",
                "entities": [
                    {
                        "entity_id": "order-1",
                        "entity_type": "order",
                        "values": [
                            {
                                "value_id": "value-1",
                                "field_name": "total_amount",
                                "raw_value": {"kind": "string", "value": "1.20"},
                                "normalized_value": {
                                    "kind": "decimal",
                                    "value": "1.20",
                                },
                                "semantic_type": "money",
                                "source_refs": [physical_ref],
                                "transformations": ["parse_decimal"],
                                "issue_codes": [],
                            }
                        ],
                        "source_refs": [physical_ref],
                        "parent_entity_id": None,
                    }
                ],
                "source_refs": [physical_ref],
                "parent_record_id": None,
                "related_record_ids": [],
            }
        ],
        "is_last": False,
        "manifest": None,
    }
    expected_json = json.dumps(
        expected_wire,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    batch = NormalizedBatch.model_validate_json(expected_json)
    serialized = batch.model_dump_json()
    restored = NormalizedBatch.model_validate_json(serialized)
    value = restored.records[0].entities[0].values[0]

    assert serialized == expected_json
    assert restored == batch
    assert type(value.raw_value) is StringScalar
    assert type(value.normalized_value) is DecimalScalar
    assert value.normalized_value.value == Decimal("1.20")
    assert value.source_refs[0].kind is PhysicalObjectKind.BLOCK
    assert restored.schema_version == "1.0.0"
    assert restored.producer.component_version == "1.0.0"
    assert restored.parse_plan_fingerprint == PARSE_PLAN_FINGERPRINT


def test_terminal_extracted_batch_requires_consistent_manifest_and_locations() -> None:
    source = _source_ref()
    location = LineRangeLocation(
        source=source,
        line_start=1,
        line_end=1,
    )
    batch = ExtractedBatch(
        extraction_id="extraction-1",
        batch_index=0,
        source=source,
        parser_id="parser.csv",
        parser_version="1.0.0",
        batch_fingerprint=BATCH_FINGERPRINT,
        lines=(),
        blocks=(
            ExtractedBlock(
                block_id="block-1",
                kind=ExtractedBlockKind.LINE,
                order=0,
                location=location,
                text="125500.50",
            ),
        ),
        tables=(
            ExtractedTable(
                table_id="table-1",
                location=location,
            ),
        ),
        trees=(),
        is_last=True,
        manifest=_extracted_manifest(),
    )

    assert batch.manifest is not None
    assert batch.manifest.source_index.refs == (
        _physical_ref(),
        _physical_ref(kind=PhysicalObjectKind.TABLE, local_id="table-1"),
    )
    assert location.source == source

    with pytest.raises(ValidationError):
        ExtractedBatch(
            extraction_id="extraction-1",
            batch_index=0,
            source=source,
            parser_id="parser.csv",
            parser_version="1.0.0",
            batch_fingerprint=BATCH_FINGERPRINT,
            is_last=True,
        )

    mixed_manifest = _extracted_manifest().model_copy(
        update={"extraction_id": "extraction-2"}
    )
    with pytest.raises(ValidationError):
        ExtractedBatch(
            extraction_id="extraction-1",
            batch_index=0,
            source=source,
            parser_id="parser.csv",
            parser_version="1.0.0",
            batch_fingerprint=BATCH_FINGERPRINT,
            is_last=True,
            manifest=mixed_manifest,
        )


def test_manifest_rejects_gaps_duplicates_and_unverifiable_refs() -> None:
    with pytest.raises(ValidationError):
        ExtractedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batches=(
                _extracted_summary(
                    batch_index=1,
                    batch_fingerprint="3" * 64,
                ),
            ),
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            source_index=ExtractedSourceIndex(refs=(_physical_ref(),)),
        )

    with pytest.raises(ValidationError):
        ExtractedSourceIndex(refs=(_physical_ref(), _physical_ref()))

    with pytest.raises(ValidationError):
        ExtractedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batches=(_extracted_summary(batch_fingerprint="3" * 64),),
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            source_index=ExtractedSourceIndex(
                refs=(
                    PhysicalSourceRef(
                        extraction_id="extraction-2",
                        batch_index=0,
                        kind=PhysicalObjectKind.BLOCK,
                        local_id="block-1",
                    ),
                )
            ),
        )


@pytest.mark.parametrize(
    "indices",
    (
        (),
        (0, 0),
        (1, 0),
        (0, 2),
    ),
)
def test_dataset_manifests_reject_duplicate_reordered_and_gapped_batches(
    indices: tuple[int, ...],
) -> None:
    extracted_batches = tuple(
        _extracted_summary(
            batch_index=index,
            batch_fingerprint="sha256:" + str(position + 3) * 64,
        )
        for position, index in enumerate(indices)
    )
    normalized_batches = tuple(
        _normalized_summary(
            batch_index=index,
            batch_fingerprint="sha256:" + str(position + 3) * 64,
        )
        for position, index in enumerate(indices)
    )

    with pytest.raises(ValidationError):
        ExtractedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batches=extracted_batches,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            source_index=ExtractedSourceIndex(),
        )
    with pytest.raises(ValidationError):
        NormalizedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            producer=_producer("parse-plan-executor"),
            batches=normalized_batches,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
        )


def test_two_batch_manifests_accept_matching_terminal_batches() -> None:
    extracted_terminal_fingerprint = "sha256:" + "3" * 64
    first_extracted = ExtractedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batch_index=0,
        batch_fingerprint=BATCH_FINGERPRINT,
    )
    extracted_terminal_draft = ExtractedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batch_index=1,
        batch_fingerprint=extracted_terminal_fingerprint,
    )
    extracted_manifest = ExtractedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batches=(
            first_extracted.to_summary(),
            extracted_terminal_draft.to_summary(),
        ),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(),
    )
    terminal_extracted = ExtractedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batch_index=1,
        batch_fingerprint=extracted_terminal_fingerprint,
        is_last=True,
        manifest=extracted_manifest,
    )

    normalized_terminal_fingerprint = "sha256:" + "4" * 64
    first_normalized = NormalizedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batch_index=0,
        batch_fingerprint=BATCH_FINGERPRINT,
    )
    normalized_terminal_draft = NormalizedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batch_index=1,
        batch_fingerprint=normalized_terminal_fingerprint,
    )
    normalized_manifest = NormalizedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batches=(
            first_normalized.to_summary(),
            normalized_terminal_draft.to_summary(),
        ),
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
    )
    terminal_normalized = NormalizedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batch_index=1,
        batch_fingerprint=normalized_terminal_fingerprint,
        is_last=True,
        manifest=normalized_manifest,
    )

    assert (first_extracted.batch_index, terminal_extracted.batch_index) == (0, 1)
    assert first_extracted.manifest is None
    assert terminal_extracted.manifest is not None
    assert terminal_extracted.manifest == extracted_manifest
    assert tuple(
        batch.batch_index for batch in terminal_extracted.manifest.batches
    ) == (0, 1)
    assert (first_normalized.batch_index, terminal_normalized.batch_index) == (0, 1)
    assert first_normalized.manifest is None
    assert terminal_normalized.manifest is not None
    assert terminal_normalized.manifest == normalized_manifest
    assert tuple(
        batch.batch_index for batch in terminal_normalized.manifest.batches
    ) == (0, 1)
    extracted_manifest.validate_batches((first_extracted, terminal_extracted))
    normalized_manifest.validate_batches((first_normalized, terminal_normalized))


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    (
        (
            "source",
            SourceArtifactRef(
                artifact_id="source-2",
                source_fingerprint="9" * 64,
            ),
        ),
        ("extraction_id", "extraction-2"),
        ("parser_id", "parser.json"),
        ("parser_version", "2.0.0"),
    ),
)
def test_terminal_extracted_batch_rejects_manifest_from_another_run(
    field_name: str,
    foreign_value: object,
) -> None:
    manifest = ExtractedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batches=(_extracted_summary(),),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(),
    )

    with pytest.raises(ValidationError):
        ExtractedBatch(
            source=_source_ref(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batch_index=0,
            batch_fingerprint=BATCH_FINGERPRINT,
            is_last=True,
            manifest=manifest.model_copy(update={field_name: foreign_value}),
        )


@pytest.mark.parametrize(
    ("field_name", "foreign_value"),
    (
        (
            "source",
            SourceArtifactRef(
                artifact_id="source-2",
                source_fingerprint="9" * 64,
            ),
        ),
        ("extraction_id", "extraction-2"),
        ("extraction_fingerprint", "9" * 64),
        ("parse_plan_fingerprint", "9" * 64),
        ("producer", _producer("foreign-parse-plan-executor")),
    ),
)
def test_terminal_normalized_batch_rejects_manifest_from_another_run(
    field_name: str,
    foreign_value: object,
) -> None:
    manifest = _normalized_manifest()

    with pytest.raises(ValidationError):
        NormalizedBatch(
            source=_source_ref(),
            extraction_id="extraction-1",
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            producer=_producer("parse-plan-executor"),
            batch_index=0,
            batch_fingerprint=BATCH_FINGERPRINT,
            is_last=True,
            manifest=manifest.model_copy(update={field_name: foreign_value}),
        )


def test_parse_plan_validation_request_rejects_broken_lineage() -> None:
    block_ref = _physical_ref()
    table_ref = _physical_ref(
        kind=PhysicalObjectKind.TABLE,
        local_id="table-1",
    )
    profile = StructureProfile(
        profile_id="profile-1",
        source=_source_ref(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        producer=_producer("structure-profiler"),
        evidence=(block_ref, table_ref),
        observations=(
            _structure_evidence(block_ref),
            _structure_evidence(table_ref),
        ),
    )

    from structuraguard.contracts import (
        ParsePlanValidationRequest,
        StructureAnalysisRequest,
    )

    plan = _parse_plan()
    request = ParsePlanValidationRequest(
        plan=plan,
        source=_source_ref(),
        manifest=_extracted_manifest(),
        profile=profile,
    )
    assert request.plan.fingerprint == plan.fingerprint

    with pytest.raises(ValidationError):
        ParsePlanValidationRequest(
            plan=_rehash_parse_plan(
                _parse_plan(),
                source_fingerprint="9" * 64,
            ),
            source=_source_ref(),
            manifest=_extracted_manifest(),
            profile=profile,
        )

    unknown_ref = _physical_ref(
        kind=PhysicalObjectKind.TABLE,
        local_id="table-999",
    )
    with pytest.raises(ValidationError):
        ParsePlanValidationRequest(
            plan=_rehash_parse_plan(_parse_plan(), evidence=(unknown_ref,)),
            source=_source_ref(),
            manifest=_extracted_manifest(),
            profile=profile,
        )

    analysis_request = StructureAnalysisRequest(
        source=_source_ref(),
        manifest=_extracted_manifest(),
        profile=profile,
        samples=(_physical_sample(),),
    )
    assert analysis_request.sample_refs == (_physical_ref(),)
    with pytest.raises(ValidationError):
        unknown_sample_ref = _physical_ref(local_id="block-999")
        StructureAnalysisRequest(
            source=_source_ref(),
            manifest=_extracted_manifest(),
            profile=profile,
            samples=(_physical_sample(unknown_sample_ref),),
        )
    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=SourceArtifactRef(
                artifact_id="source-2",
                source_fingerprint="9" * 64,
            ),
            manifest=_extracted_manifest(),
            profile=profile,
            samples=(_physical_sample(),),
        )
    foreign_profile = StructureProfile.model_validate(
        {
            **profile.model_dump(mode="python"),
            "extraction_fingerprint": "9" * 64,
        }
    )
    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source_ref(),
            manifest=_extracted_manifest(),
            profile=foreign_profile,
            samples=(_physical_sample(),),
        )


def test_normalized_batch_requires_parse_plan_and_matching_provenance() -> None:
    value = NormalizedValue(
        value_id="value-1",
        field_name="total_amount",
        raw_value=StringScalar(value="125500.50"),
        normalized_value=DecimalScalar(value=Decimal("125500.50")),
        semantic_type="money",
        source_refs=(_physical_ref(),),
        transformations=("parse_decimal",),
    )
    entity = SemanticEntity(
        entity_id="order-1",
        entity_type="order",
        values=(value,),
        source_refs=(_physical_ref(),),
    )
    record = NormalizedRecord(
        record_id="record-1",
        entities=(entity,),
        source_refs=(_physical_ref(),),
    )
    manifest = NormalizedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batches=(
            _normalized_summary(
                record_count=1,
                entity_count=1,
                value_count=1,
            ),
        ),
        record_count=1,
        entity_count=1,
        value_count=1,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        semantic_fields=("total_amount",),
        semantic_index=SemanticSourceIndex(
            fields=(
                SemanticFieldRef(
                    entity_type="order",
                    field_name="total_amount",
                ),
            )
        ),
        semantic_schema=(
            SemanticField(
                entity_type="order",
                field_name="total_amount",
                semantic_type="money",
            ),
        ),
    )
    batch = NormalizedBatch(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batch_index=0,
        batch_fingerprint=BATCH_FINGERPRINT,
        records=(record,),
        is_last=True,
        manifest=manifest,
    )

    assert batch.records[0].entities[0].values[0].normalized_value.value == Decimal(
        "125500.50"
    )

    with pytest.raises(ValidationError):
        NormalizedValue(
            value_id="value-2",
            field_name="total_amount",
            raw_value=StringScalar(value="1"),
            normalized_value=IntegerScalar(value=1),
            semantic_type="money",
            source_refs=(_physical_ref(),),
        )

    with pytest.raises(ValidationError):
        NormalizedBatch(
            source=_source_ref(),
            extraction_id="extraction-2",
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            producer=_producer("parse-plan-executor"),
            batch_index=0,
            batch_fingerprint=BATCH_FINGERPRINT,
            records=(record,),
        )


def test_database_catalog_and_mapping_plan_remain_declarative() -> None:
    column = ColumnCatalog(
        column_id="orders.total_amount",
        name="total_amount",
        type_name="numeric",
        nullable=False,
        primary_key=False,
        unique=False,
    )
    table = TableCatalog(
        table_id="public.orders",
        schema_name="public",
        name="orders",
        columns=(column,),
    )
    catalog = DatabaseCatalog(
        dialect="postgresql",
        target_id="main",
        database_fingerprint=DATABASE_FINGERPRINT,
        target_policy_fingerprint=POLICY_FINGERPRINT,
        producer=_producer("database-inspector"),
        schemas=(SchemaCatalog(schema_id="public", name="public", tables=(table,)),),
    )

    from structuraguard.contracts import (
        CatalogColumnRef,
        FieldMapping,
        LoadOperation,
        SemanticFieldRef,
    )

    mapping = FieldMapping(
        source=SemanticFieldRef(entity_type="order", field_name="total_amount"),
        target=CatalogColumnRef(
            table_id="public.orders",
            column_id="orders.total_amount",
        ),
    )
    plan = MappingPlan(
        plan_id="mapping-plan-1",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        mappings=(mapping,),
        operation=LoadOperation.INSERT_ONLY,
        confidence=Decimal("1"),
        producer=_producer("deterministic.mapper"),
    )

    assert catalog.schemas[0].tables[0].columns[0] == column
    assert plan.mappings[0].source.field_name == "total_amount"
    assert plan.fingerprint == plan.content_fingerprint()
    assert "sql" not in type(plan).model_fields
    assert "source_path" not in type(plan).model_fields


def test_parse_and_mapping_plan_payloads_are_not_cross_deserializable() -> None:
    from structuraguard.contracts import ParsePlan

    with pytest.raises(ValidationError):
        MappingPlan.model_validate(_parse_plan().model_dump(mode="python"))
    with pytest.raises(ValidationError):
        TypeAdapter(ParsePlan).validate_python(
            _mapping_plan().model_dump(mode="python")
        )


@pytest.mark.parametrize("unknown_reference", ("semantic", "catalog"))
def test_mapping_validation_rejects_unknown_index_references(
    unknown_reference: str,
) -> None:
    from structuraguard.contracts import (
        CatalogColumnRef,
        FieldMapping,
        MappingPlanValidationRequest,
        MappingPolicyRef,
    )

    plan = _mapping_plan()
    mapping = plan.mappings[0]
    if unknown_reference == "semantic":
        mapping = FieldMapping(
            source=SemanticFieldRef(
                entity_type="order",
                field_name="missing_field",
            ),
            target=mapping.target,
        )
    else:
        mapping = FieldMapping(
            source=mapping.source,
            target=CatalogColumnRef(
                table_id="public.orders",
                column_id="orders.missing_column",
            ),
        )

    with pytest.raises(ValidationError):
        MappingPlanValidationRequest(
            plan=_rehash_mapping_plan(plan, mappings=(mapping,)),
            manifest=_normalized_manifest(),
            catalog=_database_catalog(),
            policy=MappingPolicyRef(
                policy_id="mapping-policy-1",
                policy_fingerprint=POLICY_FINGERPRINT,
            ),
        )


def test_mapping_validation_binds_target_and_full_result_evidence() -> None:
    from structuraguard.contracts import (
        CatalogColumnRef,
        FieldMapping,
        IssueSeverity,
        LoadOperation,
        MappingPlanValidationRequest,
        MappingPlanValidationResult,
        MappingPolicyRef,
        ValidatedMappingPlan,
        ValidationDecision,
        ValidationIssue,
    )

    field_ref = SemanticFieldRef(entity_type="order", field_name="total_amount")
    manifest = NormalizedDatasetManifest(
        source=_source_ref(),
        extraction_id="extraction-1",
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        producer=_producer("parse-plan-executor"),
        batches=(_normalized_summary(),),
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        semantic_fields=("total_amount",),
        semantic_index=SemanticSourceIndex(fields=(field_ref,)),
        semantic_schema=(
            SemanticField(
                entity_type="order",
                field_name="total_amount",
                semantic_type="money",
            ),
        ),
    )
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
    catalog = DatabaseCatalog(
        dialect="postgresql",
        target_id="main",
        database_fingerprint=DATABASE_FINGERPRINT,
        target_policy_fingerprint=POLICY_FINGERPRINT,
        producer=_producer("database-inspector"),
        schemas=(SchemaCatalog(schema_id="public", name="public", tables=(table,)),),
    )
    mapping = FieldMapping(
        source=field_ref,
        target=CatalogColumnRef(
            table_id="public.orders",
            column_id="orders.total_amount",
        ),
    )
    plan = MappingPlan(
        plan_id="mapping-plan-1",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        mappings=(mapping,),
        operation=LoadOperation.INSERT_ONLY,
        confidence=Decimal("1"),
        producer=_producer("deterministic.mapper"),
    )
    policy = MappingPolicyRef(
        policy_id="mapping-policy-1",
        policy_fingerprint=POLICY_FINGERPRINT,
    )

    request = MappingPlanValidationRequest(
        plan=plan,
        manifest=manifest,
        catalog=catalog,
        policy=policy,
    )
    assert request.plan.target_id == request.catalog.target_id

    with pytest.raises(ValidationError):
        MappingPlanValidationRequest(
            plan=_rehash_mapping_plan(plan, target_id="other-target"),
            manifest=manifest,
            catalog=catalog,
            policy=policy,
        )

    validated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    validated = ValidatedMappingPlan(
        plan=plan,
        validator_id="mapping-plan-validator",
        validator_version="1.0.0",
        validated_at=validated_at,
        validation_fingerprint="6" * 64,
        plan_fingerprint=plan.fingerprint,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
    )
    for field_name in (
        "plan_fingerprint",
        "normalized_fingerprint",
        "database_fingerprint",
        "target_policy_fingerprint",
    ):
        payload = validated.model_dump(mode="python")
        payload[field_name] = "9" * 64
        with pytest.raises(ValidationError):
            ValidatedMappingPlan.model_validate(payload)
    with pytest.raises(ValidationError):
        ValidatedMappingPlan.model_validate(
            {**validated.model_dump(mode="python"), "target_id": "other-target"}
        )

    result = MappingPlanValidationResult(
        validator_id="mapping-plan-validator",
        validator_version="1.0.0",
        validated_at=validated_at,
        validation_fingerprint=validated.validation_fingerprint,
        plan_fingerprint=plan.fingerprint,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        decision=ValidationDecision.ACCEPTED,
        validated_plan=validated,
    )
    assert (
        MappingPlanValidationResult.model_validate_json(result.model_dump_json())
        == result
    )

    error = ValidationIssue(
        code="MAPPING_PLAN_INVALID",
        severity=IssueSeverity.ERROR,
        message_key="MAPPING_PLAN_INVALID.VALIDATION_FAILED",
    )
    rejected = MappingPlanValidationResult.model_validate(
        {
            **result.model_dump(mode="python"),
            "decision": ValidationDecision.REJECTED,
            "issues": (error,),
            "validated_plan": None,
        }
    )
    assert rejected.validated_plan is None
    with pytest.raises(ValidationError):
        MappingPlanValidationResult.model_validate(
            {
                **rejected.model_dump(mode="python"),
                "validated_plan": validated,
            }
        )

    with pytest.raises(ValidationError):
        MappingPlanValidationResult(
            validator_id="mapping-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="6" * 64,
            plan_fingerprint=plan.fingerprint,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            database_fingerprint=DATABASE_FINGERPRINT,
            target_id="main",
            target_policy_fingerprint=POLICY_FINGERPRINT,
            decision=ValidationDecision.REJECTED,
        )


def test_audit_and_report_datetimes_reject_naive_values() -> None:
    from structuraguard.contracts import (
        AuditEvent,
        IssueSeverity,
        PipelineStatus,
        SecurityReport,
        ValidationIssue,
    )

    event = AuditEvent(
        event_id=AUDIT_EVENT_ID,
        run_id=AUDIT_RUN_ID,
        occurred_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        status=PipelineStatus.CREATED,
        event_type="run_created",
        artifact_fingerprints=(SOURCE_FINGERPRINT,),
        producer=_producer("audit-emitter"),
    )
    assert AuditEvent.model_validate_json(event.model_dump_json()) == event

    terminal_payload = {
        **event.model_dump(mode="python"),
        "status": PipelineStatus.COMPLETED,
        "event_type": "run_completed",
    }
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(terminal_payload)

    approval = _security_approval('{"sample":"masked"}', run_id=AUDIT_RUN_ID)
    report = approval.report
    report_fingerprint = approval.report_fingerprint
    completed_event = AuditEvent.model_validate(
        {
            **terminal_payload,
            "artifact_fingerprints": (SOURCE_FINGERPRINT, report_fingerprint),
            "redaction_fingerprint": report.redaction_fingerprint,
            "security_report": report,
            "security_report_fingerprint": report_fingerprint,
        }
    )
    assert completed_event.security_report == report

    future_report = report.model_copy(
        update={"generated_at": datetime(2026, 9, 4, 12, 0, tzinfo=UTC)}
    )
    future_fingerprint = sha256_fingerprint(future_report)
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                **completed_event.model_dump(mode="python"),
                "artifact_fingerprints": (SOURCE_FINGERPRINT, future_fingerprint),
                "security_report": future_report,
                "security_report_fingerprint": future_fingerprint,
            }
        )

    issue = ValidationIssue(
        code="PROMPT_INJECTION_DETECTED",
        severity=IssueSeverity.ERROR,
        message_key="PROMPT_INJECTION_DETECTED.BLOCKED",
    )
    blocked_report = SecurityReport.model_validate(
        {
            **report.model_dump(mode="python"),
            "decision": "blocked",
            "status": PipelineStatus.REJECTED_SECURITY,
            "blocked_items": 1,
            "issues": (issue,),
        }
    )
    blocked_fingerprint = sha256_fingerprint(blocked_report)
    rejected_event = AuditEvent.model_validate(
        {
            **event.model_dump(mode="python"),
            "status": PipelineStatus.REJECTED_SECURITY,
            "event_type": "security_rejected",
            "artifact_fingerprints": (SOURCE_FINGERPRINT, blocked_fingerprint),
            "redaction_fingerprint": blocked_report.redaction_fingerprint,
            "security_report": blocked_report,
            "security_report_fingerprint": blocked_fingerprint,
        }
    )
    assert rejected_event.security_report is not None
    assert rejected_event.security_report.decision == "blocked"

    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                **rejected_event.model_dump(mode="python"),
                "status": PipelineStatus.LOADING,
            }
        )

    warning_issue = issue.model_copy(update={"severity": IssueSeverity.WARNING})
    warning_report = SecurityReport.model_validate(
        {
            **report.model_dump(mode="python"),
            "status": PipelineStatus.COMPLETED_WITH_WARNINGS,
            "issues": (warning_issue,),
        }
    )
    warning_fingerprint = sha256_fingerprint(warning_report)
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                **completed_event.model_dump(mode="python"),
                "artifact_fingerprints": (SOURCE_FINGERPRINT, warning_fingerprint),
                "security_report": warning_report,
                "security_report_fingerprint": warning_fingerprint,
            }
        )

    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                **event.model_dump(mode="python"),
                "artifact_fingerprints": (SOURCE_FINGERPRINT, report_fingerprint),
                "redaction_fingerprint": report.redaction_fingerprint,
                "security_report": report,
                "security_report_fingerprint": report_fingerprint,
            }
        )

    with pytest.raises(ValidationError):
        AuditEvent(
            event_id=AUDIT_EVENT_ID,
            run_id=AUDIT_RUN_ID,
            occurred_at=datetime(2026, 9, 2, 12, 0),
            status=PipelineStatus.CREATED,
            event_type="run_created",
            artifact_fingerprints=(SOURCE_FINGERPRINT,),
            producer=_producer("audit-emitter"),
        )


def test_all_parse_plan_variants_round_trip_through_the_closed_union() -> None:
    from structuraguard.contracts import (
        DocumentBlockGrouping,
        DocumentParsePlan,
        DocumentTargetSelector,
        EveryLineStart,
        LogLineGrouping,
        LogParsePlan,
        LogTokenSelector,
        ParsePlan,
        TreeNodeGrouping,
        TreeParsePlan,
        TreePathSelector,
    )

    tabular = _parse_plan()

    def base_for(
        reference: PhysicalSourceRef,
        *,
        selector: ParseFieldSelector,
        grouping: ParseEntityGrouping,
    ) -> dict[str, object]:
        payload = tabular.model_dump(mode="python")
        for field_name in (
            "kind",
            "fingerprint",
            "table_ref",
            "header_row",
            "data_start_row",
            "data_end_row",
            "footer_start_row",
            "repeated_header_rows",
        ):
            payload.pop(field_name)
        payload["fields"] = (
            ParseField(
                field_id="field-1",
                semantic_name="field_1",
                semantic_type="string",
                source_refs=(reference,),
                selector=selector,
            ),
        )
        payload["entities"] = (
            ParseEntity(
                entity_id="entity-1",
                entity_type="entity_1",
                field_ids=("field-1",),
                grouping=grouping,
            ),
        )
        payload["evidence"] = (reference,)
        return payload

    tree_ref = _physical_ref(
        kind=PhysicalObjectKind.TREE_NODE,
        local_id="node-1",
    )
    line_ref = _physical_ref(kind=PhysicalObjectKind.LINE, local_id="line-1")
    block_ref = _physical_ref(kind=PhysicalObjectKind.BLOCK, local_id="block-1")
    variants = (
        tabular,
        TreeParsePlan.model_validate(
            {
                **base_for(
                    tree_ref,
                    selector=TreePathSelector(relative_path=("value",)),
                    grouping=TreeNodeGrouping(record_path=("items",)),
                ),
                "root_ref": tree_ref,
                "record_path": ("items",),
            }
        ),
        LogParsePlan.model_validate(
            {
                **base_for(
                    line_ref,
                    selector=LogTokenSelector(token_index=0),
                    grouping=LogLineGrouping(
                        strategy="one_line",
                        start=EveryLineStart(),
                    ),
                ),
                "line_refs": (line_ref,),
                "max_lines_per_record": 10,
            }
        ),
        DocumentParsePlan.model_validate(
            {
                **base_for(
                    block_ref,
                    selector=DocumentTargetSelector(
                        target="block_text",
                        block_offset=0,
                    ),
                    grouping=DocumentBlockGrouping(strategy="per_block"),
                ),
                "block_refs": (block_ref,),
            }
        ),
    )
    adapter: TypeAdapter[ParsePlan] = TypeAdapter(ParsePlan)

    for variant in variants:
        serialized = adapter.dump_json(variant)
        restored = adapter.validate_json(serialized)

        assert type(restored) is type(variant)
        assert restored == variant
        assert adapter.dump_json(restored) == serialized


@pytest.mark.parametrize(
    "mutation",
    (
        lambda payload: payload.pop("kind"),
        lambda payload: payload.update(kind="unknown"),
        lambda payload: payload.update(
            kind="tree",
            root_ref=_physical_ref(
                kind=PhysicalObjectKind.TREE_NODE,
                local_id="node-1",
            ),
        ),
    ),
)
def test_parse_plan_union_rejects_missing_unknown_and_hybrid_variants(
    mutation: Callable[[dict[str, object]], object],
) -> None:
    from structuraguard.contracts import ParsePlan

    payload = _parse_plan().model_dump(mode="python")
    mutation(payload)

    with pytest.raises(ValidationError):
        TypeAdapter(ParsePlan).validate_python(payload)


def test_structure_review_rejects_candidates_not_present_in_profile() -> None:
    from structuraguard.contracts import (
        IssueSeverity,
        ParsePlanKind,
        StructureCandidate,
        StructureNeedsReview,
        ValidationIssue,
    )

    candidate = StructureCandidate(
        candidate_id="candidate-1",
        source=_source_ref(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        plan_kind=ParsePlanKind.TABULAR,
        confidence=Decimal("0.8"),
        evidence=(_physical_ref(),),
    )
    profile = StructureProfile(
        profile_id="profile-1",
        source=_source_ref(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        producer=_producer("structure-profiler"),
        evidence=(_physical_ref(),),
        observations=(_structure_evidence(_physical_ref()),),
        candidates=(candidate,),
    )
    rogue = candidate.model_copy(update={"candidate_id": "candidate-2"})
    issue = ValidationIssue(
        code="AMBIGUOUS_STRUCTURE",
        severity=IssueSeverity.WARNING,
        message_key="AMBIGUOUS_STRUCTURE.CANDIDATE_REVIEW_REQUIRED",
        source_refs=(_physical_ref(),),
    )

    with pytest.raises(ValidationError):
        StructureNeedsReview(
            profile=profile,
            candidates=(rogue,),
            issues=(issue,),
        )


def test_structure_analysis_outcomes_round_trip_without_fabricated_plan() -> None:
    from structuraguard.contracts import (
        IssueSeverity,
        ParsePlanKind,
        StructureAnalysisResult,
        StructureCandidate,
        StructureNeedsReview,
        StructurePlanCreated,
        StructureRejected,
        ValidationIssue,
    )

    candidate = StructureCandidate(
        candidate_id="candidate-1",
        source=_source_ref(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        plan_kind=ParsePlanKind.TABULAR,
        confidence=Decimal("0.8"),
        evidence=(_physical_ref(kind=PhysicalObjectKind.TABLE, local_id="table-1"),),
    )
    profile = StructureProfile(
        profile_id="profile-1",
        source=_source_ref(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        producer=_producer("structure-profiler"),
        evidence=candidate.evidence,
        observations=(_structure_evidence(candidate.evidence[0]),),
        candidates=(candidate,),
    )
    issue = ValidationIssue(
        code="AMBIGUOUS_STRUCTURE",
        severity=IssueSeverity.WARNING,
        message_key="AMBIGUOUS_STRUCTURE.REVIEW_REQUIRED",
        source_refs=candidate.evidence,
    )
    created = StructurePlanCreated(profile=profile, plan=_parse_plan())
    outcomes = (
        created,
        StructureNeedsReview(
            profile=profile,
            candidates=(candidate,),
            issues=(issue,),
        ),
        StructureRejected(profile=profile, issues=(issue,)),
    )
    adapter: TypeAdapter[StructureAnalysisResult] = TypeAdapter(StructureAnalysisResult)

    for outcome in outcomes:
        serialized = adapter.dump_json(outcome)
        restored = adapter.validate_json(serialized)

        assert type(restored) is type(outcome)
        assert restored == outcome
        if isinstance(restored, StructureNeedsReview | StructureRejected):
            assert not hasattr(restored, "plan")

    for field_name in (
        "source_fingerprint",
        "extraction_fingerprint",
        "profile_fingerprint",
    ):
        with pytest.raises(ValidationError):
            StructurePlanCreated(
                profile=profile,
                plan=_rehash_parse_plan(_parse_plan(), **{field_name: "9" * 64}),
            )


def test_parse_plan_validation_outcomes_require_consistent_evidence() -> None:
    from structuraguard.contracts import (
        IssueSeverity,
        ParsePlanValidationResult,
        ValidatedParsePlan,
        ValidationDecision,
        ValidationIssue,
    )

    validated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    plan = _parse_plan()
    validated = ValidatedParsePlan(
        plan=plan,
        validator_id="parse-plan-validator",
        validator_version="1.0.0",
        validated_at=validated_at,
        validation_fingerprint="3" * 64,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        plan_fingerprint=plan.fingerprint,
    )
    accepted = ParsePlanValidationResult(
        validator_id="parse-plan-validator",
        validator_version="1.0.0",
        validated_at=validated_at,
        validation_fingerprint="3" * 64,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        plan_fingerprint=plan.fingerprint,
        decision=ValidationDecision.ACCEPTED,
        validated_plan=validated,
    )
    assert (
        ParsePlanValidationResult.model_validate_json(accepted.model_dump_json())
        == accepted
    )
    for field_name in (
        "source_fingerprint",
        "extraction_fingerprint",
        "profile_fingerprint",
        "plan_fingerprint",
    ):
        payload = validated.model_dump(mode="python")
        payload[field_name] = "9" * 64
        with pytest.raises(ValidationError):
            ValidatedParsePlan.model_validate(payload)

    with pytest.raises(ValidationError):
        ParsePlanValidationResult(
            validator_id="parse-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="3" * 64,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            plan_fingerprint=plan.fingerprint,
            decision=ValidationDecision.REJECTED,
        )

    warning = ValidationIssue(
        code="AMBIGUOUS_STRUCTURE",
        severity=IssueSeverity.WARNING,
        message_key="AMBIGUOUS_STRUCTURE.VALIDATOR_WARNING",
        source_refs=(_physical_ref(),),
    )
    with pytest.raises(ValidationError):
        ParsePlanValidationResult(
            validator_id="parse-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="3" * 64,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            plan_fingerprint=plan.fingerprint,
            decision=ValidationDecision.ACCEPTED,
            issues=(warning,),
            validated_plan=validated,
        )

    error = warning.model_copy(
        update={"severity": IssueSeverity.ERROR, "source_refs": ()}
    )
    rejected = ParsePlanValidationResult.model_validate(
        {
            **accepted.model_dump(mode="python"),
            "decision": ValidationDecision.REJECTED,
            "issues": (error,),
            "validated_plan": None,
        }
    )
    assert rejected.validated_plan is None
    with pytest.raises(ValidationError):
        ParsePlanValidationResult.model_validate(
            {
                **rejected.model_dump(mode="python"),
                "validated_plan": validated,
            }
        )


def test_reports_round_trip_and_reject_impossible_load_outcome() -> None:
    from structuraguard.contracts import (
        LoadOperation,
        LoadReport,
        PipelineStatus,
        SecurityReport,
        SemanticParseReport,
        TransactionOutcome,
        ValidationDecision,
        ValidationReport,
    )

    generated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    reports = (
        SemanticParseReport(
            run_id="run-1",
            producer=_producer("parse-plan-executor"),
            status=PipelineStatus.COMPLETED,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            records=1,
            provenance_coverage=Decimal("1"),
            generated_at=generated_at,
        ),
        ValidationReport(
            run_id="run-1",
            producer=_producer("record-validator"),
            status=PipelineStatus.COMPLETED,
            decision=ValidationDecision.ACCEPTED,
            artifact_fingerprints=(NORMALIZED_FINGERPRINT,),
            total_records=1,
            valid_records=1,
            generated_at=generated_at,
        ),
        LoadReport(
            run_id="run-1",
            producer=_producer("database-loader"),
            status=PipelineStatus.COMPLETED,
            operation=LoadOperation.INSERT_ONLY,
            transaction_outcome=TransactionOutcome.COMMITTED,
            dry_run=False,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            mapping_plan_fingerprint=MAPPING_PLAN_FINGERPRINT,
            mapping_validation_fingerprint=MAPPING_VALIDATION_FINGERPRINT,
            database_fingerprint=DATABASE_FINGERPRINT,
            target_id="main",
            target_policy_fingerprint=POLICY_FINGERPRINT,
            artifact_fingerprints=LOAD_ARTIFACT_FINGERPRINTS,
            attempted_records=1,
            loaded_records=1,
            generated_at=generated_at,
        ),
        SecurityReport(
            request_id="security-request-1",
            run_id="run-1",
            purpose="source_content",
            content_fingerprint=SOURCE_FINGERPRINT,
            payload_fingerprint=BATCH_FINGERPRINT,
            data_classification=DataClassification.INTERNAL,
            routing_policy_id="security-policy-1",
            routing_policy_fingerprint=POLICY_FINGERPRINT,
            redaction_fingerprint="1" * 64,
            producer=_producer("security-scanner"),
            decision="allowed",
            status=PipelineStatus.COMPLETED,
            artifact_fingerprints=(SOURCE_FINGERPRINT, BATCH_FINGERPRINT),
            scanned_items=1,
            generated_at=generated_at,
        ),
    )

    for report in reports:
        serialized = report.model_dump_json()
        restored = type(report).model_validate_json(serialized)

        assert restored == report
        assert restored.model_dump_json() == serialized

    with pytest.raises(ValidationError):
        LoadReport(
            run_id="run-1",
            producer=_producer("database-loader"),
            status=PipelineStatus.COMPLETED,
            operation=LoadOperation.INSERT_ONLY,
            transaction_outcome=TransactionOutcome.NOT_STARTED,
            dry_run=False,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            mapping_plan_fingerprint=MAPPING_PLAN_FINGERPRINT,
            mapping_validation_fingerprint=MAPPING_VALIDATION_FINGERPRINT,
            database_fingerprint=DATABASE_FINGERPRINT,
            target_id="main",
            target_policy_fingerprint=POLICY_FINGERPRINT,
            artifact_fingerprints=LOAD_ARTIFACT_FINGERPRINTS,
            attempted_records=1,
            loaded_records=1,
            generated_at=generated_at,
        )

    with pytest.raises(ValidationError):
        SemanticParseReport(
            run_id="run-1",
            producer=_producer("parse-plan-executor"),
            status=PipelineStatus.LOADING,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            provenance_coverage=Decimal("1"),
            generated_at=generated_at,
        )

    with pytest.raises(ValidationError):
        SecurityReport(
            request_id="security-request-1",
            run_id="run-1",
            purpose="source_content",
            content_fingerprint=SOURCE_FINGERPRINT,
            payload_fingerprint=BATCH_FINGERPRINT,
            data_classification=DataClassification.INTERNAL,
            routing_policy_id="security-policy-1",
            routing_policy_fingerprint=POLICY_FINGERPRINT,
            redaction_fingerprint="1" * 64,
            producer=_producer("security-scanner"),
            decision="allowed",
            status=PipelineStatus.COMPLETED,
            artifact_fingerprints=(SOURCE_FINGERPRINT, BATCH_FINGERPRINT),
            scanned_items=1,
            blocked_items=1,
            generated_at=generated_at,
        )


def test_report_status_issue_and_security_evidence_matrix_is_consistent() -> None:
    from structuraguard.contracts import (
        IssueSeverity,
        LoadOperation,
        LoadReport,
        PipelineStatus,
        SecurityReport,
        SemanticParseReport,
        TransactionOutcome,
        ValidationDecision,
        ValidationIssue,
        ValidationReport,
    )

    generated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    error = ValidationIssue(
        code="PROVENANCE_INVALID",
        severity=IssueSeverity.ERROR,
        message_key="PROVENANCE_INVALID.VALIDATION_FAILED",
    )
    critical = error.model_copy(update={"severity": IssueSeverity.CRITICAL})
    warning = error.model_copy(update={"severity": IssueSeverity.WARNING})

    semantic_payload: dict[str, object] = {
        "run_id": "run-1",
        "producer": _producer("parse-plan-executor"),
        "status": PipelineStatus.COMPLETED,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "extraction_fingerprint": EXTRACTION_FINGERPRINT,
        "parse_plan_fingerprint": PARSE_PLAN_FINGERPRINT,
        "normalized_fingerprint": NORMALIZED_FINGERPRINT,
        "provenance_coverage": Decimal("1"),
        "generated_at": generated_at,
    }
    with pytest.raises(ValidationError):
        SemanticParseReport.model_validate({**semantic_payload, "issues": (error,)})
    for warning_state in (
        {"issues": (warning,)},
        {"unresolved_blocks": 1},
        {"provenance_coverage": Decimal("0.5")},
    ):
        with pytest.raises(ValidationError):
            SemanticParseReport.model_validate({**semantic_payload, **warning_state})
    with pytest.raises(ValidationError):
        SemanticParseReport.model_validate(
            {
                **semantic_payload,
                "status": PipelineStatus.COMPLETED_WITH_WARNINGS,
            }
        )
    warning_semantic = SemanticParseReport.model_validate(
        {
            **semantic_payload,
            "status": PipelineStatus.COMPLETED_WITH_WARNINGS,
            "unresolved_blocks": 1,
            "issues": (warning,),
        }
    )
    assert warning_semantic.status is PipelineStatus.COMPLETED_WITH_WARNINGS
    with pytest.raises(ValidationError):
        SemanticParseReport.model_validate(
            {
                **semantic_payload,
                "status": PipelineStatus.NEEDS_REVIEW,
                "normalized_fingerprint": None,
            }
        )

    cancelled_validation = ValidationReport(
        run_id="run-1",
        producer=_producer("record-validator"),
        status=PipelineStatus.CANCELLED,
        decision=ValidationDecision.REJECTED,
        artifact_fingerprints=(NORMALIZED_FINGERPRINT,),
        issues=(error,),
        generated_at=generated_at,
    )
    assert cancelled_validation.status is PipelineStatus.CANCELLED
    with pytest.raises(ValidationError):
        ValidationReport(
            run_id="run-1",
            producer=_producer("record-validator"),
            status=PipelineStatus.COMPLETED,
            decision=ValidationDecision.ACCEPTED,
            artifact_fingerprints=(NORMALIZED_FINGERPRINT,),
            total_records=10,
            valid_records=1,
            generated_at=generated_at,
        )

    failed_dry_run = LoadReport(
        run_id="run-1",
        producer=_producer("database-loader"),
        status=PipelineStatus.FAILED,
        operation=LoadOperation.INSERT_ONLY,
        transaction_outcome=TransactionOutcome.NOT_STARTED,
        dry_run=True,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
        normalized_fingerprint=NORMALIZED_FINGERPRINT,
        mapping_plan_fingerprint=MAPPING_PLAN_FINGERPRINT,
        mapping_validation_fingerprint=MAPPING_VALIDATION_FINGERPRINT,
        database_fingerprint=DATABASE_FINGERPRINT,
        target_id="main",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        artifact_fingerprints=LOAD_ARTIFACT_FINGERPRINTS,
        attempted_records=1,
        issues=(error,),
        generated_at=generated_at,
    )
    assert failed_dry_run.loaded_records == 0

    with pytest.raises(ValidationError):
        LoadReport(
            run_id="run-1",
            producer=_producer("database-loader"),
            status=PipelineStatus.ROLLED_BACK,
            operation=LoadOperation.INSERT_ONLY,
            transaction_outcome=TransactionOutcome.ROLLED_BACK,
            dry_run=False,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            mapping_plan_fingerprint=MAPPING_PLAN_FINGERPRINT,
            mapping_validation_fingerprint=MAPPING_VALIDATION_FINGERPRINT,
            database_fingerprint=DATABASE_FINGERPRINT,
            target_id="main",
            target_policy_fingerprint=POLICY_FINGERPRINT,
            artifact_fingerprints=LOAD_ARTIFACT_FINGERPRINTS,
            attempted_records=1,
            generated_at=generated_at,
        )

    completed_load: dict[str, object] = {
        "run_id": "run-1",
        "producer": _producer("database-loader"),
        "status": PipelineStatus.COMPLETED,
        "operation": LoadOperation.INSERT_ONLY,
        "transaction_outcome": TransactionOutcome.COMMITTED,
        "dry_run": False,
        "source_fingerprint": SOURCE_FINGERPRINT,
        "extraction_fingerprint": EXTRACTION_FINGERPRINT,
        "parse_plan_fingerprint": PARSE_PLAN_FINGERPRINT,
        "normalized_fingerprint": NORMALIZED_FINGERPRINT,
        "mapping_plan_fingerprint": MAPPING_PLAN_FINGERPRINT,
        "mapping_validation_fingerprint": MAPPING_VALIDATION_FINGERPRINT,
        "database_fingerprint": DATABASE_FINGERPRINT,
        "target_id": "main",
        "target_policy_fingerprint": POLICY_FINGERPRINT,
        "artifact_fingerprints": LOAD_ARTIFACT_FINGERPRINTS,
        "attempted_records": 1,
        "loaded_records": 1,
        "generated_at": generated_at,
    }
    for warning_state in (
        {"issues": (warning,)},
        {"loaded_records": 0, "rejected_records": 1},
    ):
        with pytest.raises(ValidationError):
            LoadReport.model_validate({**completed_load, **warning_state})
    with pytest.raises(ValidationError):
        LoadReport.model_validate(
            {
                **completed_load,
                "status": PipelineStatus.COMPLETED_WITH_WARNINGS,
            }
        )
    with pytest.raises(ValidationError):
        LoadReport.model_validate({**completed_load, "attempted_records": 10})

    completed_dry_run = LoadReport.model_validate(
        {
            **completed_load,
            "transaction_outcome": TransactionOutcome.DRY_RUN,
            "dry_run": True,
            "loaded_records": 0,
            "would_load_records": 1,
        }
    )
    assert completed_dry_run.would_load_records == 1
    with pytest.raises(ValidationError):
        LoadReport.model_validate(
            {
                **completed_load,
                "transaction_outcome": TransactionOutcome.DRY_RUN,
                "dry_run": True,
                "loaded_records": 0,
            }
        )
    with pytest.raises(ValidationError):
        LoadReport.model_validate({**completed_load, "would_load_records": 1})

    with pytest.raises(ValidationError):
        LoadReport(
            run_id="run-1",
            producer=_producer("database-loader"),
            status=PipelineStatus.COMPLETED,
            operation=LoadOperation.INSERT_ONLY,
            transaction_outcome=TransactionOutcome.COMMITTED,
            dry_run=False,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
            mapping_plan_fingerprint=MAPPING_PLAN_FINGERPRINT,
            mapping_validation_fingerprint=MAPPING_VALIDATION_FINGERPRINT,
            database_fingerprint=DATABASE_FINGERPRINT,
            target_id="main",
            target_policy_fingerprint=POLICY_FINGERPRINT,
            artifact_fingerprints=LOAD_ARTIFACT_FINGERPRINTS,
            attempted_records=1,
            loaded_records=1,
            issues=(error,),
            generated_at=generated_at,
        )

    with pytest.raises(ValidationError):
        LoadReport.model_validate(
            {
                **completed_load,
                "artifact_fingerprints": tuple(
                    fingerprint
                    for fingerprint in LOAD_ARTIFACT_FINGERPRINTS
                    if fingerprint != MAPPING_VALIDATION_FINGERPRINT
                ),
            }
        )

    security_payload: dict[str, object] = {
        "request_id": "security-request-1",
        "run_id": "run-1",
        "purpose": "llm_input",
        "content_fingerprint": SOURCE_FINGERPRINT,
        "payload_fingerprint": BATCH_FINGERPRINT,
        "data_classification": "INTERNAL",
        "routing_policy_id": "security-policy-1",
        "routing_policy_fingerprint": POLICY_FINGERPRINT,
        "redaction_fingerprint": "1" * 64,
        "producer": _producer("security-scanner"),
        "decision": "allowed",
        "status": PipelineStatus.COMPLETED,
        "artifact_fingerprints": (SOURCE_FINGERPRINT, BATCH_FINGERPRINT),
        "scanned_items": 1,
        "generated_at": generated_at,
    }
    for mutation in (
        {"scanned_items": 0},
        {"issues": (critical,)},
        {"artifact_fingerprints": (BATCH_FINGERPRINT,)},
        {"artifact_fingerprints": (SOURCE_FINGERPRINT,)},
    ):
        with pytest.raises(ValidationError):
            SecurityReport.model_validate({**security_payload, **mutation})


def test_fingerprint_aliases_have_one_canonical_identity() -> None:
    bare = "a" * 64
    prefixed = f"sha256:{bare}"

    assert SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=bare,
    ) == SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=prefixed,
    )
    assert (
        SourceArtifactRef(
            artifact_id="source-1",
            source_fingerprint=bare,
        ).source_fingerprint
        == prefixed
    )

    with pytest.raises(ValidationError):
        ExtractedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            parser_id="parser.csv",
            parser_version="1.0.0",
            batches=(
                _extracted_summary(batch_fingerprint=bare),
                _extracted_summary(
                    batch_index=1,
                    batch_fingerprint=prefixed,
                ),
            ),
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            source_index=ExtractedSourceIndex(),
        )
    with pytest.raises(ValidationError):
        NormalizedDatasetManifest(
            source=_source_ref(),
            extraction_id="extraction-1",
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            parse_plan_fingerprint=PARSE_PLAN_FINGERPRINT,
            producer=_producer("parse-plan-executor"),
            batches=(
                _normalized_summary(batch_fingerprint=bare),
                _normalized_summary(
                    batch_index=1,
                    batch_fingerprint=prefixed,
                ),
            ),
            normalized_fingerprint=NORMALIZED_FINGERPRINT,
        )


def test_extracted_cell_coordinates_and_parent_table_match_location() -> None:
    from structuraguard.contracts import (
        ExtractedCell,
        ExtractedValue,
        TabularCellLocation,
    )

    location = TabularCellLocation(
        source=_source_ref(),
        table_id="other-table",
        row_index=9,
        column_index=7,
    )
    value = ExtractedValue(
        value_id="value-1",
        raw_value=StringScalar(value="raw"),
        location=location,
    )

    with pytest.raises(ValidationError):
        ExtractedCell(
            cell_id="cell-1",
            row_index=0,
            column_index=0,
            value=value,
        )

    matching_value = value.model_copy(
        update={
            "location": location.model_copy(update={"row_index": 0, "column_index": 0})
        }
    )
    cell = ExtractedCell(
        cell_id="cell-1",
        row_index=0,
        column_index=0,
        value=matching_value,
    )
    with pytest.raises(ValidationError):
        ExtractedTable(
            table_id="expected-table",
            location=LineRangeLocation(
                source=_source_ref(),
                line_start=1,
                line_end=1,
            ),
            cells=(cell,),
        )

    from structuraguard.contracts import SheetCellLocation

    sheet_a = SheetCellLocation(
        source=_source_ref(),
        sheet_name="A",
        row_index=0,
        column_index=0,
    )
    sheet_b = sheet_a.model_copy(update={"sheet_name": "B"})
    mixed_sheet_cell = ExtractedCell(
        cell_id="sheet-cell-1",
        row_index=0,
        column_index=0,
        value=ExtractedValue(
            value_id="sheet-value-1",
            raw_value=StringScalar(value="raw"),
            location=sheet_b,
        ),
    )
    with pytest.raises(ValidationError):
        ExtractedTable(
            table_id="sheet-table",
            location=sheet_a,
            cells=(mixed_sheet_cell,),
        )


def test_table_catalog_rejects_conflicting_primary_key_representations() -> None:
    primary_column = ColumnCatalog(
        column_id="orders.id",
        name="id",
        type_name="integer",
        nullable=False,
        primary_key=True,
        unique=True,
    )
    regular_column = primary_column.model_copy(update={"primary_key": False})

    with pytest.raises(ValidationError):
        TableCatalog(
            table_id="public.orders",
            schema_name="public",
            name="orders",
            columns=(primary_column,),
        )

    with pytest.raises(ValidationError):
        TableCatalog(
            table_id="public.orders",
            schema_name="public",
            name="orders",
            columns=(regular_column,),
            primary_key=(regular_column.column_id,),
        )


def test_mapping_contract_binds_checked_plan_to_target_identity() -> None:
    from structuraguard.contracts import MappingCandidate, ValidatedMappingPlan

    for model in (MappingCandidate, MappingPlan, ValidatedMappingPlan):
        assert "target_id" in model.model_fields


def test_persisted_aggregate_contracts_record_schema_and_producer_versions() -> None:
    from structuraguard.contracts import (
        AuditEvent,
        LoadReport,
        SecurityReport,
        SemanticParseReport,
        ValidationReport,
    )

    for model in (
        StructureProfile,
        NormalizedDatasetManifest,
        NormalizedBatch,
        DatabaseCatalog,
        SemanticParseReport,
        ValidationReport,
        LoadReport,
        SecurityReport,
        AuditEvent,
    ):
        assert "schema_version" in model.model_fields
        assert "producer" in model.model_fields


def test_llm_request_binds_canonical_payload_to_security_evidence() -> None:
    from structuraguard.contracts import LLMRequest

    payload_json = '{"sample":"masked"}'
    payload_fingerprint = (
        "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    )
    request = LLMRequest(
        request_id="request-1",
        run_id="run-1",
        purpose="semantic_parsing",
        response_schema_id="parse-plan",
        response_schema_version="1.0.0",
        payload_json=payload_json,
        payload_fingerprint=payload_fingerprint,
        content_fingerprint=SOURCE_FINGERPRINT,
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="policy-1",
        routing_policy_fingerprint="4" * 64,
        redaction_fingerprint="1" * 64,
        security_approval=_security_approval(payload_json),
        prompt_fingerprint="2" * 64,
        max_output_bytes=4096,
    )

    assert request.payload_fingerprint == payload_fingerprint
    assert request.model_validate_json(request.model_dump_json()) == request

    with pytest.raises(ValidationError):
        request.model_copy(
            update={"payload_fingerprint": "sha256:" + "9" * 64}
        ).__class__.model_validate(
            {
                **request.model_dump(mode="python"),
                "payload_fingerprint": "sha256:" + "9" * 64,
            }
        )

    approval = _security_approval(payload_json)
    mismatched_report = approval.report.model_copy(
        update={"payload_fingerprint": "sha256:" + "9" * 64}
    )
    with pytest.raises(ValidationError):
        LLMRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "security_approval": {
                    "report": mismatched_report,
                    "report_fingerprint": sha256_fingerprint(mismatched_report),
                },
            }
        )

    for field_name, mismatched_value in (
        ("run_id", "run-other"),
        ("purpose", "llm_output"),
        ("data_classification", DataClassification.RESTRICTED),
        ("routing_policy_id", "policy-other"),
        ("routing_policy_fingerprint", "sha256:" + "8" * 64),
        ("redaction_fingerprint", "sha256:" + "7" * 64),
    ):
        mismatched_report = approval.report.model_copy(
            update={field_name: mismatched_value}
        )
        with pytest.raises(ValidationError):
            LLMRequest.model_validate(
                {
                    **request.model_dump(mode="python"),
                    "security_approval": {
                        "report": mismatched_report,
                        "report_fingerprint": sha256_fingerprint(mismatched_report),
                    },
                }
            )

    with pytest.raises(ValidationError):
        LLMRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "content_fingerprint": "sha256:" + "9" * 64,
            }
        )

    with pytest.raises(ValidationError):
        LLMRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "security_approval": {
                    "report": approval.report,
                    "report_fingerprint": "sha256:" + "9" * 64,
                },
            }
        )


def test_llm_response_binds_provider_output_and_round_trips() -> None:
    from structuraguard.contracts import LLMResponse

    output_json = '{"kind":"plan_created"}'
    output_fingerprint = (
        "sha256:" + hashlib.sha256(output_json.encode("utf-8")).hexdigest()
    )
    response = LLMResponse(
        request_id="request-1",
        provider_id="provider-1",
        provider_version="1.0.0",
        model_id="model-1",
        response_schema_id="structure-analysis-result",
        response_schema_version="1.0.0",
        output_json=output_json,
        prompt_fingerprint="2" * 64,
        generation_fingerprint=output_fingerprint,
        finish_reason="stop",
        input_tokens=10,
        output_tokens=5,
        generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )

    assert LLMResponse.model_validate_json(response.model_dump_json()) == response

    with pytest.raises(ValidationError):
        LLMResponse.model_validate(
            {
                **response.model_dump(mode="python"),
                "generation_fingerprint": "9" * 64,
            }
        )


def test_security_scan_request_binds_payload_without_source_handles() -> None:
    from structuraguard.contracts import SecurityScanRequest

    payload_json = '{"sample":"untrusted"}'
    payload_fingerprint = (
        "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    )
    request = SecurityScanRequest(
        request_id="security-request-1",
        run_id="run-1",
        purpose="source_content",
        content_fingerprint=SOURCE_FINGERPRINT,
        payload_json=payload_json,
        payload_fingerprint=payload_fingerprint,
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="security-policy-1",
        routing_policy_fingerprint=POLICY_FINGERPRINT,
        redaction_fingerprint="1" * 64,
    )

    assert SecurityScanRequest.model_validate_json(request.model_dump_json()) == request
    assert {"source_handle", "database_handle", "credentials"}.isdisjoint(
        SecurityScanRequest.model_fields
    )

    with pytest.raises(ValidationError):
        SecurityScanRequest.model_validate(
            {
                **request.model_dump(mode="python"),
                "payload_fingerprint": "9" * 64,
            }
        )


@pytest.mark.parametrize(
    "secret",
    (
        "postgresql://alice:secret@db.internal/app",
        "Authorization: Bearer secret-token",
        "password=hunter2",
        "-----BEGIN PRIVATE KEY-----",
        "sk-proj-supersecret",
        "AKIAIOSFODNN7EXAMPLE",
        "ASIAIOSFODNN7EXAMPLE",
        "sk_live_51N1234567890ABCDE",
        "whsec_1234567890ABCDEFGHIJ",
    ),
)
def test_llm_request_rejects_credential_canaries_inside_values(secret: str) -> None:
    from structuraguard.contracts import LLMRequest

    payload_json = json.dumps(
        {"sample": secret},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValidationError):
        LLMRequest(
            request_id="request-1",
            run_id="run-1",
            purpose="semantic_parsing",
            response_schema_id="parse-plan",
            response_schema_version="1.0.0",
            payload_json=payload_json,
            payload_fingerprint=(
                "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
            ),
            content_fingerprint=SOURCE_FINGERPRINT,
            data_classification=DataClassification.INTERNAL,
            routing_policy_id="policy-1",
            routing_policy_fingerprint="4" * 64,
            redaction_fingerprint="1" * 64,
            security_approval=_security_approval(payload_json),
            prompt_fingerprint="2" * 64,
            max_output_bytes=4096,
        )


def test_audit_identifiers_and_report_messages_reject_log_and_secret_canaries() -> None:
    from structuraguard.contracts import IssueSeverity, ValidationIssue

    with pytest.raises(ValidationError):
        SourceArtifact(
            artifact_id="source-1\nFORGED",
            display_name="orders.csv",
            media_type="text/csv",
            size_bytes=128,
            source_fingerprint=SOURCE_FINGERPRINT,
        )

    assert "message" not in ValidationIssue.model_fields
    with pytest.raises(ValidationError):
        ValidationIssue.model_validate(
            {
                "code": "PROVENANCE_INVALID",
                "severity": IssueSeverity.ERROR,
                "message_key": "PROVENANCE_INVALID.REDACTED",
                "message": "dsn=postgresql://alice:secret@db.internal/app",
            }
        )

    for sensitive_message in (
        "Связаться с alice@example.com",
        "Карта 4111-1111-1111-1111 отклонена",
    ):
        with pytest.raises(ValidationError):
            ValidationIssue.model_validate(
                {
                    "code": "PROVENANCE_INVALID",
                    "severity": IssueSeverity.ERROR,
                    "message_key": sensitive_message,
                }
            )

    from structuraguard.contracts import AuditEvent, PipelineStatus

    for sensitive_id in (
        "alice@example.com",
        "4111-1111-1111-1111",
        "sk-proj-supersecret",
        "event\u2028FORGED",
        "event\u202eFORGED",
        "+7-999-123-45-67",
        "4510 123456",
        "7707083893",
        "phone-79991234567",
        "passport-4510-123456",
        "inn-7707083893",
        "phone79991234567-550e8400-e29b-41d4-a716-446655440000",
        "alice.smith-550e8400-e29b-41d4-a716-446655440000",
        "event-ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
    ):
        with pytest.raises(ValidationError):
            AuditEvent(
                event_id=sensitive_id,
                run_id=AUDIT_RUN_ID,
                occurred_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
                status=PipelineStatus.CREATED,
                event_type="run_created",
                artifact_fingerprints=(SOURCE_FINGERPRINT,),
                producer=_producer("audit-emitter"),
            )

    safe_event = AuditEvent(
        event_id=AUDIT_EVENT_ID,
        run_id=AUDIT_RUN_ID,
        occurred_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        status=PipelineStatus.CREATED,
        event_type="run_created",
        artifact_fingerprints=(SOURCE_FINGERPRINT,),
        producer=_producer("audit-emitter"),
    )
    with pytest.raises(ValidationError):
        AuditEvent.model_validate(
            {
                **safe_event.model_dump(mode="python"),
                "producer": {
                    "component_id": "alice@example.com",
                    "component_version": "1.0.0",
                    "sdk_version": "0.2.0",
                },
            }
        )
