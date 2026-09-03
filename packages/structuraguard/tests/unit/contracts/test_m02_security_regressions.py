"""Регрессии security boundary и canonical value semantics M2."""

from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext

import pytest
from pydantic import TypeAdapter, ValidationError

from structuraguard.contracts._base import CanonicalValue, canonical_sha256_value
from structuraguard.contracts.common import (
    BooleanScalar,
    DataClassification,
    DateScalar,
    DateTimeScalar,
    DecimalScalar,
    IssueSeverity,
    LoadOperation,
    NumberScalar,
    PhysicalObjectKind,
    PhysicalSourceRef,
    PipelineStatus,
    ProducerMetadata,
    RawScalar,
    StringScalar,
    TransactionOutcome,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    TableCatalog,
)
from structuraguard.contracts.mapping import (
    FieldMapping,
    MappingCandidate,
    MappingPlan,
    MappingPlanValidationResult,
    ValidatedMappingPlan,
)
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.reports import (
    AuditEvent,
    LLMRequest,
    LLMResponse,
    LoadReport,
    SecurityApproval,
    SecurityReport,
    SecurityScanRequest,
    SemanticParseReport,
    ValidationReport,
)
from structuraguard.contracts.source import SourceArtifact

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
POLICY_FINGERPRINT = "sha256:" + "b" * 64
REDACTION_FINGERPRINT = "sha256:" + "c" * 64
PROMPT_FINGERPRINT = "sha256:" + "d" * 64
AUDIT_EVENT_ID = "event-550e8400-e29b-41d4-a716-446655440000"
AUDIT_RUN_ID = "run-550e8400-e29b-41d4-a716-446655440000"


def _text_fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _security_report(
    *,
    classification: DataClassification = DataClassification.INTERNAL,
) -> SecurityReport:
    payload_json = '{"sample":"masked"}'
    payload_fingerprint = _text_fingerprint(payload_json)
    return SecurityReport(
        request_id="security-request-1",
        run_id="run-1",
        purpose="llm_input",
        content_fingerprint=SOURCE_FINGERPRINT,
        payload_fingerprint=payload_fingerprint,
        data_classification=classification,
        routing_policy_id="policy-1",
        routing_policy_fingerprint=POLICY_FINGERPRINT,
        redaction_fingerprint=REDACTION_FINGERPRINT,
        producer=ProducerMetadata(
            component_id="security-scanner",
            component_version="1.0.0",
            sdk_version="0.2.0",
        ),
        decision="allowed",
        status=PipelineStatus.COMPLETED,
        artifact_fingerprints=(SOURCE_FINGERPRINT, payload_fingerprint),
        scanned_items=1,
        generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )


def _security_approval() -> SecurityApproval:
    report = _security_report()
    return SecurityApproval(
        report=report,
        report_fingerprint=_text_fingerprint(report.canonical_json()),
    )


def _llm_request_payload() -> dict[str, object]:
    payload_json = '{"sample":"masked"}'
    return {
        "request_id": "request-1",
        "run_id": "run-1",
        "purpose": "semantic_parsing",
        "response_schema_id": "parse-plan",
        "response_schema_version": "1.0.0",
        "payload_json": payload_json,
        "payload_fingerprint": _text_fingerprint(payload_json),
        "content_fingerprint": SOURCE_FINGERPRINT,
        "data_classification": DataClassification.INTERNAL,
        "routing_policy_id": "policy-1",
        "routing_policy_fingerprint": POLICY_FINGERPRINT,
        "redaction_fingerprint": REDACTION_FINGERPRINT,
        "security_approval": _security_approval(),
        "prompt_fingerprint": PROMPT_FINGERPRINT,
        "max_output_bytes": 4096,
    }


def test_validation_error_does_not_echo_secret_input_or_attacker_key() -> None:
    attacker_marker = "ATTACKER_CONTROLLED_MARKER"
    output_json = '{"password_' + attacker_marker + '":"hunter2"}'

    with pytest.raises(ValidationError) as captured:
        LLMResponse(
            request_id="request-1",
            provider_id="provider-1",
            provider_version="1.0.0",
            model_id="model-1",
            response_schema_id="parse-plan",
            response_schema_version="1.0.0",
            output_json=output_json,
            prompt_fingerprint=PROMPT_FINGERPRINT,
            generation_fingerprint=_text_fingerprint(output_json),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    rendered = str(captured.value)
    assert "hunter2" not in rendered
    assert attacker_marker not in rendered


def test_validation_error_structured_views_redact_input_for_all_entrypoints() -> None:
    attacker_marker = "ATTACKER_CONTROLLED_MARKER"
    output_json = '{"password_' + attacker_marker + '":"hunter2"}'
    payload: dict[str, object] = {
        "request_id": "request-1",
        "provider_id": "provider-1",
        "provider_version": "1.0.0",
        "model_id": "model-1",
        "response_schema_id": "parse-plan",
        "response_schema_version": "1.0.0",
        "output_json": output_json,
        "prompt_fingerprint": PROMPT_FINGERPRINT,
        "generation_fingerprint": _text_fingerprint(output_json),
        "finish_reason": "stop",
        "input_tokens": 1,
        "output_tokens": 1,
        "generated_at": "2026-09-03T12:00:00Z",
    }
    json_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def construct_response() -> LLMResponse:
        return LLMResponse(
            request_id="request-1",
            provider_id="provider-1",
            provider_version="1.0.0",
            model_id="model-1",
            response_schema_id="parse-plan",
            response_schema_version="1.0.0",
            output_json=output_json,
            prompt_fingerprint=PROMPT_FINGERPRINT,
            generation_fingerprint=_text_fingerprint(output_json),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    entrypoints: tuple[Callable[[], LLMResponse], ...] = (
        construct_response,
        lambda: LLMResponse.model_validate(payload),
        lambda: LLMResponse.model_validate_json(json_payload),
    )

    for invoke in entrypoints:
        with pytest.raises(ValidationError) as captured:
            invoke()

        error = captured.value
        renderings = (
            str(error),
            repr(error),
            repr(error.errors()),
            error.json(),
            "".join(traceback.format_exception(error)),
        )
        assert all("hunter2" not in rendered for rendered in renderings)
        assert all(attacker_marker not in rendered for rendered in renderings)
        assert error.errors()[0]["loc"] == ("output_json",)
        assert error.errors()[0]["input"] == "[REDACTED]"


@pytest.mark.parametrize(
    "attacker_key",
    (
        "ghp_abcdefghijklmnop",
        "AKIAIOSFODNN7EXAMPLE",
        "alice@example.com",
        "4111111111111111",
    ),
)
def test_validation_error_redacts_secret_and_pii_from_dynamic_location(
    attacker_key: str,
) -> None:
    payload: dict[str, object] = {
        "artifact_id": "source-1",
        "display_name": "orders.csv",
        "media_type": "text/csv",
        "size_bytes": 1,
        "source_fingerprint": SOURCE_FINGERPRINT,
        attacker_key: "attacker-controlled",
    }

    with pytest.raises(ValidationError) as captured:
        SourceArtifact.model_validate(payload)

    renderings = (
        str(captured.value),
        repr(captured.value.errors()),
        captured.value.json(),
        "".join(traceback.format_exception(captured.value)),
    )
    assert all(attacker_key not in rendering for rendering in renderings)
    assert captured.value.errors()[0]["loc"] == ("<redacted>",)


def test_raw_scalar_type_adapter_redacts_unknown_discriminator_input() -> None:
    attacker_marker = "ATTACKER_CONTROLLED_MARKER"

    with pytest.raises(ValidationError) as captured:
        TypeAdapter(RawScalar).validate_python(
            {
                "kind": "password_" + attacker_marker,
                "value": "hunter2",
            }
        )

    renderings = (
        str(captured.value),
        repr(captured.value.errors()),
        captured.value.json(),
    )
    assert all("hunter2" not in rendered for rendered in renderings)
    assert all(attacker_marker not in rendered for rendered in renderings)


def test_frozen_assignment_error_redacts_attempted_value() -> None:
    scalar = StringScalar(value="safe")
    secret = "password=hunter2"

    with pytest.raises(ValidationError) as captured:
        scalar.value = secret

    renderings = (
        str(captured.value),
        repr(captured.value.errors()),
        captured.value.json(),
        "".join(traceback.format_exception(captured.value)),
    )
    assert all("hunter2" not in rendered for rendered in renderings)
    assert captured.value.errors()[0]["loc"] == ("value",)
    assert captured.value.errors()[0]["input"] == "[REDACTED]"


def test_data_classification_is_closed_and_shared_by_security_contracts() -> None:
    assert tuple(DataClassification) == (
        DataClassification.PUBLIC,
        DataClassification.INTERNAL,
        DataClassification.CONFIDENTIAL,
        DataClassification.RESTRICTED,
    )
    for model in (SecurityReport, SecurityScanRequest, LLMRequest):
        assert (
            model.model_fields["data_classification"].annotation is DataClassification
        )

    report = _security_report()
    assert report.data_classification is DataClassification.INTERNAL
    assert '"data_classification":"INTERNAL"' in report.canonical_json()

    with pytest.raises(ValidationError):
        SecurityReport.model_validate(
            {
                **report.model_dump(mode="python"),
                "data_classification": "unknown-classification",
            }
        )

    scan_payload = '{"sample":"masked"}'
    with pytest.raises(ValidationError):
        SecurityScanRequest.model_validate(
            {
                "request_id": "security-request-1",
                "run_id": "run-1",
                "purpose": "llm_input",
                "content_fingerprint": SOURCE_FINGERPRINT,
                "payload_json": scan_payload,
                "payload_fingerprint": _text_fingerprint(scan_payload),
                "data_classification": "unknown-classification",
                "routing_policy_id": "policy-1",
                "routing_policy_fingerprint": POLICY_FINGERPRINT,
                "redaction_fingerprint": REDACTION_FINGERPRINT,
            }
        )

    with pytest.raises(ValidationError):
        LLMRequest.model_validate(
            {
                **_llm_request_payload(),
                "data_classification": "unknown-classification",
            }
        )

    for invalid_input in (b"PUBLIC", bytearray(b"PUBLIC")):
        with pytest.raises(ValidationError):
            SecurityScanRequest.model_validate(
                {
                    "request_id": "security-request-1",
                    "run_id": "run-1",
                    "purpose": "llm_input",
                    "content_fingerprint": SOURCE_FINGERPRINT,
                    "payload_json": scan_payload,
                    "payload_fingerprint": _text_fingerprint(scan_payload),
                    "data_classification": invalid_input,
                    "routing_policy_id": "policy-1",
                    "routing_policy_fingerprint": POLICY_FINGERPRINT,
                    "redaction_fingerprint": REDACTION_FINGERPRINT,
                }
            )


def test_canonical_json_fields_reject_python_bytes_coercion() -> None:
    payload_json = b'{"sample":"masked"}'

    with pytest.raises(ValidationError):
        SecurityScanRequest.model_validate(
            {
                "request_id": "security-request-1",
                "run_id": "run-1",
                "purpose": "llm_input",
                "content_fingerprint": SOURCE_FINGERPRINT,
                "payload_json": payload_json,
                "payload_fingerprint": _text_fingerprint(payload_json.decode("utf-8")),
                "data_classification": DataClassification.INTERNAL,
                "routing_policy_id": "policy-1",
                "routing_policy_fingerprint": POLICY_FINGERPRINT,
                "redaction_fingerprint": REDACTION_FINGERPRINT,
            }
        )


def _foreign_issue() -> ValidationIssue:
    return ValidationIssue(
        code="PROVENANCE_INVALID",
        severity=IssueSeverity.WARNING,
        message_key="PROVENANCE_INVALID.FOREIGN_REFERENCE",
        source_refs=(
            PhysicalSourceRef(
                extraction_id="extraction-2",
                batch_index=999,
                kind=PhysicalObjectKind.CELL,
                local_id="cell-1",
            ),
        ),
    )


def _mapping_plan() -> MappingPlan:
    return MappingPlan(
        plan_id="mapping-plan-1",
        revision=1,
        source_fingerprint=SOURCE_FINGERPRINT,
        extraction_fingerprint="sha256:" + "e" * 64,
        parse_plan_fingerprint="sha256:" + "f" * 64,
        normalized_fingerprint="sha256:" + "1" * 64,
        database_fingerprint="sha256:" + "2" * 64,
        target_id="target-1",
        target_policy_fingerprint=POLICY_FINGERPRINT,
        mappings=(
            FieldMapping(
                source=SemanticFieldRef(
                    entity_type="order",
                    field_name="total_amount",
                ),
                target=CatalogColumnRef(
                    table_id="orders",
                    column_id="total_amount",
                ),
            ),
        ),
        operation=LoadOperation.INSERT_ONLY,
        confidence=Decimal("1"),
        producer=ProducerMetadata(
            component_id="mapping-analyzer",
            component_version="1.0.0",
            sdk_version="0.2.0",
        ),
    )


def test_stage_reports_reject_unbound_physical_issue_references() -> None:
    issue = _foreign_issue()
    with pytest.raises(ValidationError, match="physical source_refs"):
        SemanticParseReport(
            run_id="run-1",
            producer=ProducerMetadata(
                component_id="parse-plan-executor",
                component_version="1.0.0",
                sdk_version="0.2.0",
            ),
            status=PipelineStatus.FAILED,
            source_fingerprint=SOURCE_FINGERPRINT,
            extraction_fingerprint="sha256:" + "e" * 64,
            parse_plan_fingerprint="sha256:" + "f" * 64,
            provenance_coverage=Decimal("0"),
            issues=(issue,),
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    report = _security_report().model_dump(mode="python")
    report.update(
        status=PipelineStatus.COMPLETED_WITH_WARNINGS,
        issues=(issue,),
    )
    with pytest.raises(ValidationError, match="physical source_refs"):
        SecurityReport.model_validate(report)

    with pytest.raises(ValidationError, match="physical source_refs"):
        ValidationReport(
            run_id="run-1",
            producer=ProducerMetadata(
                component_id="record-validator",
                component_version="1.0.0",
                sdk_version="0.2.0",
            ),
            status=PipelineStatus.FAILED,
            decision=ValidationDecision.REJECTED,
            artifact_fingerprints=(SOURCE_FINGERPRINT,),
            issues=(issue,),
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    load_fingerprints = (
        SOURCE_FINGERPRINT,
        "sha256:" + "e" * 64,
        "sha256:" + "f" * 64,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
        POLICY_FINGERPRINT,
    )
    with pytest.raises(ValidationError, match="physical source_refs"):
        LoadReport(
            run_id="run-1",
            producer=ProducerMetadata(
                component_id="database-loader",
                component_version="1.0.0",
                sdk_version="0.2.0",
            ),
            status=PipelineStatus.FAILED,
            operation=LoadOperation.INSERT_ONLY,
            transaction_outcome=TransactionOutcome.NOT_STARTED,
            dry_run=True,
            source_fingerprint=load_fingerprints[0],
            extraction_fingerprint=load_fingerprints[1],
            parse_plan_fingerprint=load_fingerprints[2],
            normalized_fingerprint=load_fingerprints[3],
            mapping_plan_fingerprint=load_fingerprints[4],
            mapping_validation_fingerprint=load_fingerprints[5],
            database_fingerprint=load_fingerprints[6],
            target_id="target-1",
            target_policy_fingerprint=load_fingerprints[7],
            artifact_fingerprints=load_fingerprints,
            issues=(issue,),
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )


def test_mapping_contracts_reject_physical_issue_references() -> None:
    issue = _foreign_issue()
    plan = _mapping_plan()
    source = plan.mappings[0].source
    target = plan.mappings[0].target

    with pytest.raises(ValidationError, match="physical source_refs"):
        MappingCandidate(
            candidate_id="candidate-1",
            source_fingerprint=plan.source_fingerprint,
            extraction_fingerprint=plan.extraction_fingerprint,
            parse_plan_fingerprint=plan.parse_plan_fingerprint,
            normalized_fingerprint=plan.normalized_fingerprint,
            database_fingerprint=plan.database_fingerprint,
            target_id=plan.target_id,
            target_policy_fingerprint=plan.target_policy_fingerprint,
            producer=plan.producer,
            source=source,
            target=target,
            confidence=Decimal("1"),
            issues=(issue,),
        )

    validated_at = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    wrapper_payload: dict[str, object] = {
        "plan": plan,
        "validator_id": "mapping-plan-validator",
        "validator_version": "1.0.0",
        "validated_at": validated_at,
        "validation_fingerprint": "sha256:" + "3" * 64,
        "plan_fingerprint": plan.fingerprint,
        "normalized_fingerprint": plan.normalized_fingerprint,
        "database_fingerprint": plan.database_fingerprint,
        "target_id": plan.target_id,
        "target_policy_fingerprint": plan.target_policy_fingerprint,
        "issues": (issue,),
    }
    with pytest.raises(ValidationError, match="physical source_refs"):
        ValidatedMappingPlan.model_validate(wrapper_payload)

    with pytest.raises(ValidationError, match="physical source_refs"):
        MappingPlanValidationResult(
            validator_id="mapping-plan-validator",
            validator_version="1.0.0",
            validated_at=validated_at,
            validation_fingerprint="sha256:" + "3" * 64,
            plan_fingerprint=plan.fingerprint,
            source_fingerprint=plan.source_fingerprint,
            extraction_fingerprint=plan.extraction_fingerprint,
            parse_plan_fingerprint=plan.parse_plan_fingerprint,
            normalized_fingerprint=plan.normalized_fingerprint,
            database_fingerprint=plan.database_fingerprint,
            target_id=plan.target_id,
            target_policy_fingerprint=plan.target_policy_fingerprint,
            decision=ValidationDecision.REJECTED,
            issues=(issue,),
        )


@pytest.mark.parametrize(
    ("field_name", "sensitive_value"),
    (
        ("extraction_id", "alice@example.com"),
        ("extraction_id", "alice@example.com."),
        ("local_id", "cell-4111111111111111"),
        ("local_id", "cell-4111_1111_1111_1111"),
        ("local_id", "password=hunter2"),
        ("local_id", "cell-79991234567"),
        ("local_id", "cell-alice"),
        ("local_id", "table-1"),
    ),
)
def test_physical_source_ref_rejects_sensitive_persisted_identifiers(
    field_name: str,
    sensitive_value: str,
) -> None:
    payload: dict[str, object] = {
        "extraction_id": "extraction-1",
        "batch_index": 0,
        "kind": PhysicalObjectKind.CELL,
        "local_id": "cell-1",
    }
    payload[field_name] = sensitive_value

    with pytest.raises(ValidationError):
        PhysicalSourceRef.model_validate(payload)


@pytest.mark.parametrize(
    ("kind", "local_id"),
    (
        (PhysicalObjectKind.LINE, "line-1"),
        (PhysicalObjectKind.BLOCK, "block-1"),
        (PhysicalObjectKind.TABLE, "table-1"),
        (PhysicalObjectKind.CELL, "cell-1"),
        (PhysicalObjectKind.TREE_NODE, "node-1"),
        (PhysicalObjectKind.VALUE, "value-1"),
        (PhysicalObjectKind.EXTENSION, "extension-1"),
        (
            PhysicalObjectKind.CELL,
            "cell-550e8400-e29b-41d4-a716-446655440000",
        ),
        (PhysicalObjectKind.CELL, "cell-01ARZ3NDEKTSV4RRFFQ69G5FAV"),
    ),
)
def test_physical_source_ref_accepts_only_kind_bound_generated_identifiers(
    kind: PhysicalObjectKind,
    local_id: str,
) -> None:
    reference = PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=0,
        kind=kind,
        local_id=local_id,
    )

    assert reference.local_id == local_id


@pytest.mark.parametrize(
    "comment",
    (
        "password=hunter2",
        "Authorization: Bearer secret-token",
        "secret=hunter2",
        "token=hunter2",
        "client_secret=hunter2",
        "aws_secret_access_key=hunter2",
        "Bearer secret-token-123",
        "Basic YWxpY2U6c2VjcmV0",
        "safe first line\nforged second line",
    ),
)
def test_database_catalog_rejects_secret_or_control_text(comment: str) -> None:
    with pytest.raises(ValidationError):
        ColumnCatalog(
            column_id="orders.id",
            name="id",
            type_name="integer",
            nullable=False,
            primary_key=True,
            comment=comment,
        )

    column = ColumnCatalog(
        column_id="orders.id",
        name="id",
        type_name="integer",
        nullable=False,
        primary_key=True,
    )
    with pytest.raises(ValidationError):
        TableCatalog(
            table_id="orders",
            schema_name="public",
            name="orders",
            columns=(column,),
            primary_key=(column.column_id,),
            comment=comment,
        )
    with pytest.raises(ValidationError):
        TableCatalog(
            table_id="orders",
            schema_name="public",
            name="orders",
            columns=(column,),
            primary_key=(column.column_id,),
            check_constraints=(comment,),
        )


@pytest.mark.parametrize(
    "secret",
    (
        "secret=hunter2",
        "token=hunter2",
        "client_secret=hunter2",
        "aws_secret_access_key=hunter2",
        "Bearer secret-token-123",
        "Basic YWxpY2U6c2VjcmV0",
    ),
)
def test_llm_output_and_persisted_identifiers_reject_secret_canaries(
    secret: str,
) -> None:
    output_json = json.dumps(
        {"sample": secret},
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValidationError):
        LLMResponse(
            request_id="request-1",
            provider_id="provider-1",
            provider_version="1.0.0",
            model_id="model-1",
            response_schema_id="parse-plan",
            response_schema_version="1.0.0",
            output_json=output_json,
            prompt_fingerprint=PROMPT_FINGERPRINT,
            generation_fingerprint=_text_fingerprint(output_json),
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        )

    with pytest.raises(ValidationError):
        AuditEvent(
            event_id=AUDIT_EVENT_ID,
            run_id=AUDIT_RUN_ID,
            occurred_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
            status=PipelineStatus.CREATED,
            event_type=secret,
            artifact_fingerprints=(SOURCE_FINGERPRINT,),
            producer=ProducerMetadata(
                component_id="audit-emitter",
                component_version="1.0.0",
                sdk_version="0.2.0",
            ),
        )


@pytest.mark.parametrize(
    "numeric_value",
    (0, 86_400, 0.0, Decimal("86400"), "86400"),
)
def test_temporal_scalars_reject_numeric_timestamp_coercion(
    numeric_value: object,
) -> None:
    with pytest.raises(ValidationError):
        DateScalar.model_validate({"value": numeric_value})
    with pytest.raises(ValidationError):
        DateTimeScalar.model_validate({"value": numeric_value})


@pytest.mark.parametrize(
    "value",
    (
        b"2026-09-03",
        "2026-09-03T00:00:00Z",
        "2026-09-03 00:00:00+00:00",
    ),
)
def test_date_scalar_rejects_cross_type_text_and_bytes(value: object) -> None:
    with pytest.raises(ValidationError):
        DateScalar.model_validate({"value": value})


def test_temporal_scalars_accept_only_their_exact_iso_textual_forms() -> None:
    calendar_date = DateScalar.model_validate_json(
        '{"kind":"date","value":"2026-09-03"}'
    )
    timestamp = DateTimeScalar.model_validate_json(
        '{"kind":"datetime","value":"2026-09-03T12:00:00+03:00"}'
    )

    assert calendar_date.value == date(2026, 9, 3)
    assert timestamp.value == datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    for invalid_datetime in (b"2026-09-03T12:00:00Z", "2026-09-03"):
        with pytest.raises(ValidationError):
            DateTimeScalar.model_validate({"value": invalid_datetime})


@pytest.mark.parametrize("value", (1, Decimal("0.1"), True))
def test_number_scalar_requires_an_exact_binary_float(value: object) -> None:
    with pytest.raises(ValidationError):
        NumberScalar.model_validate({"value": value})

    assert BooleanScalar(value=True).value is True
    assert NumberScalar(value=1.0).value == 1.0


def test_numeric_scalar_equality_matches_canonical_identity() -> None:
    decimal_left = DecimalScalar(value=Decimal("1.0"))
    decimal_right = DecimalScalar(value=Decimal("1.00"))
    negative_zero = NumberScalar(value=-0.0)
    positive_zero = NumberScalar(value=0.0)
    precise_left = DecimalScalar(value=Decimal("12345678901234567890123456789.00"))
    precise_right = DecimalScalar(value=Decimal("12345678901234567890123456789"))

    assert decimal_left == decimal_right
    assert decimal_left.canonical_json() == decimal_right.canonical_json()
    assert negative_zero == positive_zero
    assert negative_zero.canonical_json() == positive_zero.canonical_json()
    assert precise_left == precise_right
    assert precise_left.canonical_json() == precise_right.canonical_json()
    assert "12345678901234567890123456789" in precise_left.canonical_json()
    with localcontext() as decimal_context:
        decimal_context.prec = 3
        assert precise_left.canonical_json() == precise_right.canonical_json()

    scientific = DecimalScalar(value=Decimal("1000"))
    with localcontext() as decimal_context:
        decimal_context.capitals = 0
        lowercase_context = scientific.canonical_json()
    with localcontext() as decimal_context:
        decimal_context.capitals = 1
        uppercase_context = scientific.canonical_json()
    assert lowercase_context == uppercase_context


def test_canonical_sha256_can_exclude_self_referential_top_level_field() -> None:
    value: dict[str, CanonicalValue] = {"fingerprint": "stale", "value": 1}

    assert canonical_sha256_value(
        value,
        exclude_top_level=frozenset({"fingerprint"}),
    ) == _text_fingerprint('{"value":1}')
