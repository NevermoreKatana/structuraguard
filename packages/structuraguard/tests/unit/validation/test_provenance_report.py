"""Physical replay, trace binding и безопасный итоговый отчёт."""

from datetime import UTC, datetime

import pytest
from tests.fakes.provenance import provenance_fixture, rehash

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.provenance import ProvenancePolicy
from structuraguard.contracts.reports import ValidationReport
from structuraguard.validation import ProvenanceValidator

_NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.mark.anyio
async def test_replay_report_preserves_references_and_safe_summary() -> None:
    fixture = await provenance_fixture(
        b'[{"email":"restricted@example.org"},{"email":"second@example.org"}]'
    )
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert isinstance(report, ValidationReport)
    assert report.decision is ValidationDecision.ACCEPTED
    assert report.complete
    assert report.valid_records == report.total_records == 2
    assert report.verified_values == report.required_values == 2
    assert report.evidence[0].verified
    assert report.evidence[0].locations
    assert report.evidence[0].raw_reference.pointer.endswith("/raw_value")
    assert report.evidence[0].normalized_reference.pointer.endswith("/normalized_value")
    assert "restricted" not in report.safe_summary().model_dump_json()
    assert "email" not in report.safe_summary().model_dump_json()
    assert "restricted" not in repr(report)
    assert "restricted" not in report.model_dump_json()  # ссылки, без scalar copies


@pytest.mark.anyio
async def test_rehashed_fake_pointer_is_not_evidence() -> None:
    fixture = await provenance_fixture()
    batch = fixture.normalized[0]
    record = batch.records[0]
    entity = record.entities[0]
    value = entity.values[0]
    origin = value.origins[0]
    forged = value.model_copy(
        update={
            "origins": (
                origin.model_copy(
                    update={
                        "location": origin.location.model_copy(
                            update={"pointer": "/9/name"}
                        )
                    }
                ),
            )
        }
    )
    altered = batch.model_copy(
        update={
            "records": (
                record.model_copy(
                    update={
                        "entities": (entity.model_copy(update={"values": (forged,)}),)
                    }
                ),
            )
        }
    )
    report = await ProvenanceValidator().validate(
        rehash((altered, *fixture.normalized[1:])),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert "PROVENANCE_LOCATION_MISMATCH" in {issue.code for issue in report.issues}
    assert report.invalid_records == 1
    assert report.valid_records == 1
    assert report.decision is ValidationDecision.REJECTED


@pytest.mark.anyio
async def test_non_null_without_origins_needs_review() -> None:
    fixture = await provenance_fixture()
    batch = fixture.normalized[0]
    record = batch.records[0]
    entity = record.entities[0]
    value = entity.values[0].model_copy(update={"origins": (), "selection": None})
    altered = batch.model_copy(
        update={
            "records": (
                record.model_copy(
                    update={
                        "entities": (entity.model_copy(update={"values": (value,)}),)
                    }
                ),
            )
        }
    )
    report = await ProvenanceValidator(policy=ProvenancePolicy()).validate(
        rehash((altered, *fixture.normalized[1:])),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert "PROVENANCE_REQUIRED" in {issue.code for issue in report.issues}
    assert report.decision is ValidationDecision.NEEDS_REVIEW
    assert report.unresolved_records == 1
    assert report.valid_records == 1


@pytest.mark.anyio
async def test_normalization_trace_is_replayed_and_links_raw_locations() -> None:
    from structuraguard.contracts.normalization import NormalizerSpec
    from structuraguard.contracts.normalized import SemanticFieldRef
    from structuraguard.contracts.provenance import NormalizationBinding
    from structuraguard.normalization import NormalizerRegistry

    fixture = await provenance_fixture()
    registry = NormalizerRegistry.with_builtins().freeze()
    values = tuple(
        v
        for b in fixture.normalized
        for r in b.records
        for e in r.entities
        for v in e.values
    )
    binding = NormalizationBinding(
        field=SemanticFieldRef(entity_type="unresolved", field_name="field_0"),
        steps=(NormalizerSpec(normalizer_id="trim"),),
    )
    traces = tuple(
        registry.normalize_value(value, steps=binding.steps) for value in values
    )
    report = await ProvenanceValidator(
        policy=ProvenancePolicy(normalizations=(binding,)), registry=registry
    ).validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
        normalizations=traces,
    )
    assert report.decision is ValidationDecision.ACCEPTED
    assert traces[0].raw_value.value == " Ada "
    assert traces[0].normalized_value.value == "Ada"
    assert (
        report.evidence[0].normalized_reference.artifact_fingerprint
        == traces[0].fingerprint
    )
    assert report.evidence[0].locations[0].location == values[0].origins[0].location
    assert report.verified_values == 2


@pytest.mark.anyio
async def test_missing_normalization_is_unverified_and_not_silently_skipped() -> None:
    from structuraguard.contracts.normalization import NormalizerSpec
    from structuraguard.contracts.normalized import SemanticFieldRef
    from structuraguard.contracts.provenance import NormalizationBinding
    from structuraguard.normalization import NormalizerRegistry

    fixture = await provenance_fixture()
    binding = NormalizationBinding(
        field=SemanticFieldRef(entity_type="unresolved", field_name="field_0"),
        steps=(NormalizerSpec(normalizer_id="trim"),),
    )
    report = await ProvenanceValidator(
        policy=ProvenancePolicy(normalizations=(binding,)),
        registry=NormalizerRegistry.with_builtins().freeze(),
    ).validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.NEEDS_REVIEW
    assert report.unresolved_records == 2
    assert report.valid_records == report.invalid_records == 0
    assert [issue.code for issue in report.issues] == [
        "NORMALIZATION_EVIDENCE_MISSING"
    ] * 2


@pytest.mark.anyio
async def test_report_aggregates_all_layers_counts_records_once_and_orders_issues() -> (
    None
):
    from structuraguard.contracts.common import IssueSeverity
    from structuraguard.contracts.provenance import (
        ValidationFinding,
        ValidationLayer,
        ValidationLayerResult,
    )
    from structuraguard.validation import ValidationReportBuilder

    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    warning = ValidationFinding(
        layer=ValidationLayer.JSON_SCHEMA,
        code="SCHEMA_WARNING",
        record_index=1,
        severity=IssueSeverity.WARNING,
        outcome="warning",
    )
    error = ValidationFinding(
        layer=ValidationLayer.JSON_SCHEMA,
        code="SCHEMA_TYPE",
        record_index=0,
        check_index=1,
        json_path="$.restricted",
    )
    rule = ValidationFinding(
        layer=ValidationLayer.BUSINESS_RULES, code="BUSINESS_SUM", record_index=0
    )
    schema = ValidationLayerResult(
        layer=ValidationLayer.JSON_SCHEMA,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "a" * 64,),
        complete=True,
        findings=(warning, error, error),
    )
    rules = ValidationLayerResult(
        layer=ValidationLayer.BUSINESS_RULES,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "b" * 64,),
        complete=True,
        findings=(rule,),
    )
    builder = ValidationReportBuilder(
        required_layers=(ValidationLayer.JSON_SCHEMA, ValidationLayer.BUSINESS_RULES)
    )
    combined = builder.combine(report, layers=(rules, schema))
    assert combined.findings == (error, warning, rule)
    assert (
        combined.invalid_records
        == combined.valid_records
        == combined.warning_records
        == 1
    )
    assert combined.complete
    assert combined.evidence == report.evidence
    assert "restricted" not in combined.safe_summary().model_dump_json()
    assert (
        builder.combine(report, layers=(schema, rules)).evidence_fingerprint
        == combined.evidence_fingerprint
    )
    assert report.decision is ValidationDecision.ACCEPTED


@pytest.mark.anyio
async def test_missing_requested_layer_and_foreign_layer_input_cannot_pass() -> None:
    from structuraguard.contracts.provenance import (
        ValidationLayer,
        ValidationLayerResult,
    )
    from structuraguard.exceptions import ValidationError
    from structuraguard.validation import ValidationReportBuilder

    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    builder = ValidationReportBuilder(required_layers=(ValidationLayer.DATABASE,))
    missing = builder.combine(report)
    assert not missing.complete
    assert missing.decision is ValidationDecision.NEEDS_REVIEW
    assert missing.unresolved_records == missing.total_records == 2
    foreign = ValidationLayerResult(
        layer=ValidationLayer.DATABASE,
        input_fingerprint="sha256:" + "c" * 64,
        evidence_fingerprints=("sha256:" + "d" * 64,),
        complete=True,
    )
    with pytest.raises(ValidationError, match="VALIDATION_REPORT_INPUT_MISMATCH"):
        builder.combine(report, layers=(foreign,))


@pytest.mark.anyio
async def test_report_evidence_hash_excludes_clock_and_round_trips() -> None:
    from structuraguard.contracts.provenance import DetailedValidationReport

    fixture = await provenance_fixture()
    validator = ProvenanceValidator()
    first = await validator.validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    second = await validator.validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW.replace(day=13),
    )
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert (
        DetailedValidationReport.model_validate_json(first.model_dump_json()) == first
    )


@pytest.mark.anyio
async def test_normalized_batch_boundaries_do_not_change_record_verification() -> None:
    from tests.unit.structure.test_execution import stream

    from structuraguard.structure import ParsePlanExecutor

    fixture = await provenance_fixture()
    recut = tuple(
        [
            batch
            async for batch in ParsePlanExecutor().execute(
                stream(fixture.physical),
                fixture.plan,
                fixture.context.model_copy(update={"max_records_per_batch": 2}),
            )
        ]
    )
    assert len(recut) != len(fixture.normalized)
    before = tuple(batch.canonical_json() for batch in fixture.normalized)
    first = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    second = await ProvenanceValidator().validate(
        recut,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert first.safe_summary() == second.safe_summary()
    assert first.decision is second.decision is ValidationDecision.ACCEPTED
    assert tuple(item.value_id for item in first.evidence) == tuple(
        item.value_id for item in second.evidence
    )
    assert tuple(batch.canonical_json() for batch in fixture.normalized) == before


@pytest.mark.anyio
@pytest.mark.parametrize("require_null", [False, True])
async def test_null_has_explicit_required_evidence_policy(require_null: bool) -> None:
    from tests.fakes.provenance import replace_value

    fixture = await provenance_fixture(b'[{"name":null},{"name":null}]')
    value = fixture.normalized[0].records[0].entities[0].values[0]
    assert value.raw_value.kind == "null"
    altered = replace_value(
        fixture, value.model_copy(update={"origins": (), "selection": None})
    )
    report = await ProvenanceValidator(
        policy=ProvenancePolicy(require_null=require_null)
    ).validate(
        altered,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is (
        ValidationDecision.NEEDS_REVIEW if require_null else ValidationDecision.ACCEPTED
    )
    assert report.required_values == (2 if require_null else 0)


@pytest.mark.anyio
async def test_report_policy_binds_replay_controls() -> None:
    from tests.unit.structure.test_execution import stream

    from structuraguard.contracts.execution import ParsePlanOptions
    from structuraguard.contracts.parsing import ParsePlanValidationRequest
    from structuraguard.structure import ParsePlanValidator

    fixture = await provenance_fixture()
    original = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert fixture.context.profile is not None
    request = ParsePlanValidationRequest(
        plan=fixture.plan.plan,
        source=fixture.context.manifest.source,
        manifest=fixture.context.manifest,
        profile=fixture.context.profile,
    )
    options = ParsePlanOptions(max_records=100)
    validation = await ParsePlanValidator(options=options).validate_source(
        request, stream(fixture.physical)
    )
    assert validation.validated_plan is not None
    bounded = await ProvenanceValidator(parse_options=options).validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=validation.validated_plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert bounded.decision is ValidationDecision.ACCEPTED
    assert bounded.lineage.policy_fingerprint != original.lineage.policy_fingerprint
