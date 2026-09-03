"""Регрессии полноты и lineage semantic parsing contracts M2."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    IssueSeverity,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    SourceArtifactRef,
    StringScalar,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.parsing import (
    DocumentBlockGrouping,
    DocumentParsePlan,
    DocumentTargetSelector,
    LogLineGrouping,
    LogParsePlan,
    LogTokenSelector,
    ParseEntity,
    ParseField,
    ParsePlan,
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
    PhysicalSample,
    PrefixTokenStart,
    StructureAnalysisRequest,
    StructureEvidence,
    StructureProfile,
    StructureRejected,
    TabularColumnSelector,
    TabularParsePlan,
    TabularRowGrouping,
    TabularShapeObservation,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import (
    ExtractedBatchSummary,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    SourceLocation,
    TabularCellLocation,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
EXTRACTION_FINGERPRINT = "sha256:" + "b" * 64
PROFILE_FINGERPRINT = "sha256:" + "c" * 64
BATCH_FINGERPRINT = "sha256:" + "d" * 64


def _source() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="source-1",
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _ref(kind: PhysicalObjectKind, local_id: str) -> PhysicalSourceRef:
    return PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=0,
        kind=kind,
        local_id=local_id,
    )


def _producer(component_id: str) -> ProducerMetadata:
    return ProducerMetadata(
        component_id=component_id,
        component_version="1.0.0",
        sdk_version="0.1.0",
    )


def _manifest() -> ExtractedDatasetManifest:
    return ExtractedDatasetManifest(
        source=_source(),
        extraction_id="extraction-1",
        parser_id="parser.csv",
        parser_version="1.0.0",
        batches=(
            ExtractedBatchSummary(
                batch_index=0,
                batch_fingerprint=BATCH_FINGERPRINT,
                source=_source(),
                extraction_id="extraction-1",
                parser_id="parser.csv",
                parser_version="1.0.0",
                physical_ref_count=3,
            ),
        ),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(
            refs=(
                _ref(PhysicalObjectKind.TABLE, "table-1"),
                _ref(PhysicalObjectKind.CELL, "cell-1"),
                _ref(PhysicalObjectKind.CELL, "cell-2"),
            )
        ),
    )


def _evidence() -> StructureEvidence:
    table_ref = _ref(PhysicalObjectKind.TABLE, "table-1")
    return StructureEvidence(
        evidence_id="table-shape-1",
        code="TABULAR_SHAPE",
        source_refs=(table_ref,),
        confidence=Decimal("0.95"),
        observation=TabularShapeObservation(
            table_ref=table_ref,
            sampled_row_count=2,
            column_count=2,
            header_candidates=(0,),
        ),
    )


def _profile() -> StructureProfile:
    evidence = _evidence()
    return StructureProfile(
        profile_id="profile-1",
        source=_source(),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        producer=_producer("structure-profiler"),
        evidence=evidence.source_refs,
        observations=(evidence,),
    )


def _sample(local_id: str, raw_value: str = "125.50") -> PhysicalSample:
    index = 0 if local_id == "cell-1" else 1
    return PhysicalSample(
        source_ref=_ref(PhysicalObjectKind.CELL, local_id),
        batch_fingerprint=BATCH_FINGERPRINT,
        raw_value=StringScalar(value=raw_value),
        location=TabularCellLocation(
            source=_source(),
            table_id="table-1",
            row_index=1,
            column_index=index,
        ),
    )


def _plan() -> TabularParsePlan:
    table_ref = _ref(PhysicalObjectKind.TABLE, "table-1")
    field = ParseField(
        field_id="total-amount",
        semantic_name="total_amount",
        semantic_type="money",
        source_refs=(table_ref,),
        selector=TabularColumnSelector(column_index=1),
    )
    return TabularParsePlan(
        plan_id="parse-plan-1",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        profile_fingerprint=PROFILE_FINGERPRINT,
        confidence=Decimal("0.95"),
        producer=_producer("semantic-analyzer"),
        fields=(field,),
        entities=(
            ParseEntity(
                entity_id="order",
                entity_type="order",
                field_ids=(field.field_id,),
                grouping=TabularRowGrouping(rows_per_entity=1),
            ),
        ),
        evidence=(table_ref,),
        table_ref=table_ref,
        header_row=0,
        data_start_row=1,
    )


def test_profile_requires_typed_observations_covering_physical_evidence() -> None:
    profile = _profile()

    assert isinstance(profile.observations[0].observation, TabularShapeObservation)
    assert profile.observations[0].source_refs == profile.evidence

    payload = profile.model_dump(mode="python")
    payload["observations"] = ()
    with pytest.raises(ValidationError):
        StructureProfile.model_validate(payload)

    foreign = _evidence().model_copy(
        update={
            "source_refs": (_ref(PhysicalObjectKind.CELL, "cell-1"),),
        }
    )
    payload["observations"] = (foreign,)
    with pytest.raises(ValidationError):
        StructureProfile.model_validate(payload)


def test_analysis_request_carries_bounded_fingerprint_bound_raw_samples() -> None:
    first = _sample("cell-1")
    second = _sample("cell-2", "RUB")
    request = StructureAnalysisRequest(
        source=_source(),
        manifest=_manifest(),
        profile=_profile(),
        samples=(first, second),
        max_sample_values=2,
        max_sample_bytes=8_192,
    )

    assert request.sample_refs == (first.source_ref, second.source_ref)
    assert request.samples[0].raw_value == StringScalar(value="125.50")
    assert request.samples[0].fingerprint.startswith("sha256:")

    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
            samples=(first, second),
            max_sample_values=1,
        )

    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
            samples=(first, first),
        )

    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
            samples=(first,),
            max_sample_bytes=1,
        )


def test_analysis_request_rejects_sample_with_stale_content_or_lineage() -> None:
    sample = _sample("cell-1")
    stale_payload = sample.model_dump(mode="python")
    stale_payload["raw_value"] = StringScalar(value="tampered")
    with pytest.raises(ValidationError):
        PhysicalSample.model_validate(stale_payload)

    wrong_batch_payload = sample.model_dump(mode="python")
    wrong_batch_payload.pop("fingerprint")
    wrong_batch_payload["batch_fingerprint"] = "sha256:" + "9" * 64
    wrong_batch = PhysicalSample.model_validate(wrong_batch_payload)
    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
            samples=(wrong_batch,),
        )

    unknown_payload = sample.model_dump(mode="python")
    unknown_payload.pop("fingerprint")
    unknown_payload["source_ref"] = _ref(
        PhysicalObjectKind.CELL,
        "cell-999",
    )
    unknown = PhysicalSample.model_validate(unknown_payload)
    with pytest.raises(ValidationError):
        StructureAnalysisRequest(
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
            samples=(unknown,),
        )


def test_parse_plan_requires_closed_selector_and_complete_entity_grouping() -> None:
    plan = _plan()

    assert isinstance(plan.fields[0].selector, TabularColumnSelector)
    assert isinstance(plan.entities[0].grouping, TabularRowGrouping)

    field_payload = plan.fields[0].model_dump(mode="python")
    field_payload.pop("selector")
    with pytest.raises(ValidationError):
        ParseField.model_validate(field_payload)

    plan_payload = plan.model_dump(mode="python")
    plan_payload.pop("fingerprint")
    plan_payload.pop("entities")
    with pytest.raises(ValidationError):
        TabularParsePlan.model_validate(plan_payload)

    plan_payload = plan.model_dump(mode="python")
    plan_payload.pop("fingerprint")
    plan_payload["fields"] = (
        plan.fields[0].model_copy(update={"selector": LogTokenSelector(token_index=0)}),
    )
    with pytest.raises(ValidationError):
        TabularParsePlan.model_validate(plan_payload)


def test_all_plan_variants_define_executor_complete_selector_and_grouping() -> None:
    tabular = _plan()

    def variant_payload(reference: PhysicalSourceRef) -> dict[str, object]:
        payload = tabular.model_dump(mode="python")
        for field_name in (
            "fingerprint",
            "kind",
            "table_ref",
            "header_row",
            "data_start_row",
            "data_end_row",
            "footer_start_row",
            "repeated_header_rows",
        ):
            payload.pop(field_name)
        payload["evidence"] = (reference,)
        return payload

    tree_ref = _ref(PhysicalObjectKind.TREE_NODE, "node-1")
    tree_payload = variant_payload(tree_ref)
    tree_field = ParseField(
        field_id="name",
        semantic_name="name",
        semantic_type="string",
        source_refs=(tree_ref,),
        selector=TreePathSelector(relative_path=("customer", "name")),
    )
    tree_payload.update(
        fields=(tree_field,),
        entities=(
            ParseEntity(
                entity_id="customer",
                entity_type="customer",
                field_ids=(tree_field.field_id,),
                grouping=TreeNodeGrouping(record_path=("customers",)),
            ),
        ),
        root_ref=tree_ref,
        record_path=("customers",),
    )
    tree = TreeParsePlan.model_validate(tree_payload)

    line_ref = _ref(PhysicalObjectKind.LINE, "line-1")
    log_payload = variant_payload(line_ref)
    log_field = ParseField(
        field_id="level",
        semantic_name="level",
        semantic_type="string",
        source_refs=(line_ref,),
        selector=LogTokenSelector(token_index=0, delimiter="whitespace"),
    )
    log_payload.update(
        fields=(log_field,),
        entities=(
            ParseEntity(
                entity_id="log-entry",
                entity_type="log_entry",
                field_ids=(log_field.field_id,),
                grouping=LogLineGrouping(
                    strategy="fixed_lines",
                    start=PrefixTokenStart(token="ENTRY"),
                    lines_per_record=2,
                ),
            ),
        ),
        line_refs=(line_ref,),
        max_lines_per_record=2,
    )
    log = LogParsePlan.model_validate(log_payload)

    block_ref = _ref(PhysicalObjectKind.BLOCK, "block-1")
    document_payload = variant_payload(block_ref)
    document_field = ParseField(
        field_id="title",
        semantic_name="title",
        semantic_type="string",
        source_refs=(block_ref,),
        selector=DocumentTargetSelector(target="block_text", block_offset=0),
    )
    document_payload.update(
        fields=(document_field,),
        entities=(
            ParseEntity(
                entity_id="section",
                entity_type="section",
                field_ids=(document_field.field_id,),
                grouping=DocumentBlockGrouping(strategy="per_block"),
            ),
        ),
        block_refs=(block_ref,),
    )
    document = DocumentParsePlan.model_validate(document_payload)

    adapter: TypeAdapter[ParsePlan] = TypeAdapter(ParsePlan)
    for variant in (tabular, tree, log, document):
        restored = adapter.validate_json(adapter.dump_json(variant))
        assert restored == variant

    conflicting_tree_payload = tree.model_dump(mode="python")
    conflicting_tree_payload.pop("fingerprint")
    conflicting_tree_payload["entities"] = (
        tree.entities[0].model_copy(
            update={"grouping": TreeNodeGrouping(record_path=("other",))}
        ),
    )
    with pytest.raises(ValidationError, match="record_path"):
        TreeParsePlan.model_validate(conflicting_tree_payload)

    out_of_range_log = log.model_dump(mode="python")
    out_of_range_log.pop("fingerprint")
    out_of_range_log["fields"] = (
        log.fields[0].model_copy(
            update={
                "selector": LogTokenSelector(
                    line_offset=2,
                    token_index=0,
                )
            }
        ),
    )
    with pytest.raises(ValidationError, match="line_offset"):
        LogParsePlan.model_validate(out_of_range_log)

    out_of_range_document = document.model_dump(mode="python")
    out_of_range_document.pop("fingerprint")
    out_of_range_document["fields"] = (
        document.fields[0].model_copy(
            update={
                "selector": DocumentTargetSelector(
                    target="block_text",
                    block_offset=1,
                )
            }
        ),
    )
    with pytest.raises(ValidationError, match="block_offset"):
        DocumentParsePlan.model_validate(out_of_range_document)


@pytest.mark.parametrize(
    ("data_end_row", "footer_start_row", "repeated_header_row"),
    (
        (2, None, 3),
        (None, 3, 3),
    ),
)
def test_tabular_plan_bounds_repeated_headers_to_data_region(
    data_end_row: int | None,
    footer_start_row: int | None,
    repeated_header_row: int,
) -> None:
    payload = _plan().model_dump(mode="python")
    payload.pop("fingerprint")
    payload.update(
        data_end_row=data_end_row,
        footer_start_row=footer_start_row,
        repeated_header_rows=(repeated_header_row,),
    )

    with pytest.raises(ValidationError, match="repeated header"):
        TabularParsePlan.model_validate(payload)


def test_parse_field_selector_rejects_regex_and_executable_extension_fields() -> None:
    payload = _plan().fields[0].model_dump(mode="python")
    payload["selector"] = {
        "kind": "regex",
        "pattern": "(?P<value>.*)",
        "callback": "module:function",
    }

    with pytest.raises(ValidationError):
        ParseField.model_validate(payload)


def test_parse_plan_fingerprint_is_derived_and_rejects_stale_payload() -> None:
    plan = _plan()
    payload = plan.model_dump(mode="python")
    payload["data_start_row"] = 2

    with pytest.raises(ValidationError):
        TabularParsePlan.model_validate(payload)

    payload.pop("fingerprint")
    changed = TabularParsePlan.model_validate(payload)
    assert changed.fingerprint != plan.fingerprint

    stale = plan.model_copy(update={"data_start_row": 2})
    with pytest.raises(ValidationError):
        ParsePlanValidationRequest(
            plan=stale,
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
        )

    invalid_field = plan.fields[0].model_copy(
        update={"selector": LogTokenSelector(token_index=0)}
    )
    invalid_plan = plan.model_copy(update={"fields": (invalid_field,)})
    forged_fingerprint = canonical_sha256_value(
        invalid_plan,
        exclude_top_level=frozenset({"fingerprint"}),
    )
    forged_plan = invalid_plan.model_copy(update={"fingerprint": forged_fingerprint})
    with pytest.raises(ValidationError):
        ParsePlanValidationRequest(
            plan=forged_plan,
            source=_source(),
            manifest=_manifest(),
            profile=_profile(),
        )


def test_entity_grouping_rejects_unassigned_duplicate_and_dangling_fields() -> None:
    plan = _plan()
    payload = plan.model_dump(mode="python")
    payload.pop("fingerprint")

    payload["entities"] = ()
    with pytest.raises(ValidationError):
        TabularParsePlan.model_validate(payload)

    payload["entities"] = (
        ParseEntity(
            entity_id="order",
            entity_type="order",
            field_ids=("missing-field",),
            grouping=TabularRowGrouping(),
        ),
    )
    with pytest.raises(ValidationError):
        TabularParsePlan.model_validate(payload)


def test_validation_outcomes_reject_unbound_issue_source_references() -> None:
    plan = _plan()
    foreign_ref = PhysicalSourceRef(
        extraction_id="extraction-2",
        batch_index=0,
        kind=PhysicalObjectKind.TABLE,
        local_id="table-1",
    )
    warning = ValidationIssue(
        code="AMBIGUOUS_STRUCTURE",
        severity=IssueSeverity.WARNING,
        message_key="AMBIGUOUS_STRUCTURE.REVIEW_REQUIRED",
        source_refs=(foreign_ref,),
    )

    with pytest.raises(ValidationError):
        StructureRejected(profile=_profile(), issues=(warning,))

    validated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    with pytest.raises(ValidationError):
        ValidatedParsePlan(
            plan=plan,
            validator_id="parse-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="sha256:" + "8" * 64,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            plan_fingerprint=plan.fingerprint,
            issues=(warning,),
        )

    with pytest.raises(ValidationError):
        ParsePlanValidationResult(
            validator_id="parse-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="sha256:" + "8" * 64,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint=EXTRACTION_FINGERPRINT,
            profile_fingerprint=PROFILE_FINGERPRINT,
            plan_fingerprint=plan.fingerprint,
            decision=ValidationDecision.REJECTED,
            issues=(warning,),
        )


def test_public_union_adapters_redact_unknown_discriminator_input() -> None:
    attacker_marker = "ATTACKER_CONTROLLED_MARKER"
    payload = {
        "kind": f"password_{attacker_marker}",
        "value": "hunter2",
    }
    adapters: tuple[TypeAdapter[SourceLocation], TypeAdapter[ParsePlan]] = (
        TypeAdapter(SourceLocation),
        TypeAdapter(ParsePlan),
    )

    for adapter in adapters:
        with pytest.raises(ValidationError) as captured:
            adapter.validate_python(payload)

        renderings = (
            str(captured.value),
            repr(captured.value.errors()),
            captured.value.json(),
        )
        assert all("hunter2" not in rendering for rendering in renderings)
        assert all(attacker_marker not in rendering for rendering in renderings)
