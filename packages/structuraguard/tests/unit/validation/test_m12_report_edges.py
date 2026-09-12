"""M12 AC-07/08: empty scope, warnings и закрытие required layers."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.fakes.provenance import provenance_fixture
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import execution_context, stream

from structuraguard.contracts.analysis import TreePathOperation, TreeStep
from structuraguard.contracts.common import (
    IssueSeverity,
    PhysicalObjectKind,
    PipelineStatus,
    ValidationDecision,
)
from structuraguard.contracts.parsing import (
    ParseEntity,
    ParseField,
    ParsePlanValidationRequest,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.contracts.provenance import (
    ProvenanceLimits,
    ValidationFinding,
    ValidationLayer,
    ValidationLayerResult,
)
from structuraguard.exceptions import ValidationError
from structuraguard.parsers.builtin import JsonDocumentParser
from structuraguard.structure import (
    ParsePlanExecutor,
    ParsePlanValidator,
    StructuralProfiler,
)
from structuraguard.validation import ProvenanceValidator, ValidationReportBuilder

_NOW = datetime(2026, 9, 13, tzinfo=UTC)


@pytest.mark.anyio
async def test_empty_verified_dataset_keeps_zero_coverage_and_no_phantom_records() -> (
    None
):
    content = b"[]"
    source = source_for(content, display_name="empty")
    physical = await collect(
        JsonDocumentParser(), source, contexts_for(source, content)[1]
    )
    profile = await StructuralProfiler().profile(stream(physical))
    manifest = physical[-1].manifest
    assert manifest is not None
    root = next(
        ref
        for ref in manifest.source_index.refs
        if ref.kind is PhysicalObjectKind.TREE_NODE
    )
    steps = (TreeStep(operation=TreePathOperation.ITEM),)
    plan = TreeParsePlan(
        plan_id="empty_report",
        schema_version="1.1.0",
        revision=1,
        source_fingerprint=manifest.source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        profile_fingerprint=profile.profile_fingerprint,
        confidence=Decimal(1),
        producer=profile.producer,
        root_ref=root,
        record_steps=steps,
        fields=(
            ParseField(
                field_id="value",
                semantic_name="value",
                semantic_type="unresolved",
                source_refs=(root,),
                selector=TreePathSelector(),
            ),
        ),
        entities=(
            ParseEntity(
                entity_id="items",
                entity_type="unresolved",
                field_ids=("value",),
                grouping=TreeNodeGrouping(record_steps=steps),
            ),
        ),
        evidence=(root,),
    )
    request = ParsePlanValidationRequest(
        plan=plan, source=manifest.source, manifest=manifest, profile=profile
    )
    validated = await ParsePlanValidator().validate_source(request, stream(physical))
    assert validated.validated_plan is not None
    context = execution_context(request)
    normalized = tuple(
        [
            batch
            async for batch in ParsePlanExecutor().execute(
                stream(physical), validated.validated_plan, context
            )
        ]
    )
    report = await ProvenanceValidator().validate(
        normalized,
        source_batches=physical,
        plan=validated.validated_plan,
        context=context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.ACCEPTED and report.complete
    assert (
        report.total_records
        == report.valid_records
        == report.invalid_records
        == report.unresolved_records
        == 0
    )
    assert report.required_values == report.verified_values == 0
    assert not report.evidence and not report.issues
    pending = ValidationReportBuilder(
        required_layers=(ValidationLayer.DATABASE,)
    ).combine(report)
    assert pending.decision is ValidationDecision.NEEDS_REVIEW
    assert (
        pending.total_records
        == pending.unresolved_records
        == pending.warning_records
        == 0
    )
    assert not pending.complete


@pytest.mark.anyio
async def test_warning_only_layer_keeps_valid_records_and_completed_with_warnings() -> (
    None
):
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    layer = ValidationLayerResult(
        layer=ValidationLayer.JSON_SCHEMA,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "a" * 64,),
        complete=True,
        findings=(
            ValidationFinding(
                layer=ValidationLayer.JSON_SCHEMA,
                code="SCHEMA_NOTICE",
                severity=IssueSeverity.WARNING,
                outcome="warning",
                record_index=1,
            ),
        ),
    )
    result = ValidationReportBuilder(
        required_layers=(ValidationLayer.JSON_SCHEMA,)
    ).combine(report, layers=(layer,))
    assert result.decision is ValidationDecision.ACCEPTED
    assert result.status is PipelineStatus.COMPLETED_WITH_WARNINGS
    assert result.valid_records == 2 and result.warning_records == 1
    assert result.invalid_records == result.unresolved_records == 0


@pytest.mark.anyio
async def test_missing_layer_findings_obey_report_issue_budget() -> None:
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    with pytest.raises(ValidationError) as error:
        ValidationReportBuilder(
            required_layers=(ValidationLayer.JSON_SCHEMA, ValidationLayer.DATABASE),
            limits=ProvenanceLimits(max_issues=1),
        ).combine(report)
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"
