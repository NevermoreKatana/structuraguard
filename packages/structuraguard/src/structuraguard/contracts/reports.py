"""Typed reports, audit events и безопасные adapter boundary DTO."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Annotated, Literal, Never, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from ._base import FrozenContract
from .common import (
    ConfidenceDecimal,
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    IssueSeverity,
    LoadOperation,
    NonNegativeInt,
    PipelineStatus,
    ProducerMetadata,
    SchemaVersionStr,
    TransactionOutcome,
    UtcDateTime,
    ValidationDecision,
    ValidationIssue,
    VersionStr,
    _contains_credential_canary,
    _exact_data_classification_input,
)

_CanonicalJson = Annotated[
    str,
    StringConstraints(strict=True, min_length=2, max_length=1_048_576),
]
_PayloadByteLimit = Annotated[StrictInt, Field(gt=0, le=1_048_576)]
_LlmPurpose = Literal["semantic_parsing", "semantic_mapping"]
_SecurityPurpose = Literal[
    "source_content",
    "semantic_sample",
    "llm_input",
    "llm_output",
    "parse_plan",
    "mapping_plan",
]
_FORBIDDEN_LLM_KEYS = frozenset(
    {
        "authorization",
        "access_token",
        "api_key",
        "api_keys",
        "api_token",
        "client_secret",
        "connection",
        "connection_string",
        "credential",
        "credentials",
        "database_handle",
        "db_handle",
        "dsn",
        "password",
        "passwd",
        "private_key",
        "pwd",
        "refresh_token",
        "secret",
        "secrets",
        "shell",
        "source_handle",
        "sql",
        "token",
        "tokens",
        "tool",
        "tool_choice",
        "tools",
    }
)
_FORBIDDEN_LLM_KEY_PARTS = frozenset(
    {
        "authorization",
        "credential",
        "credentials",
        "dsn",
        "password",
        "passwd",
        "pwd",
        "secret",
        "secrets",
        "shell",
        "sql",
        "token",
        "tokens",
        "tool",
        "tools",
    }
)
_FORBIDDEN_LLM_COMPACT_KEYS = frozenset(
    key.replace("_", "") for key in _FORBIDDEN_LLM_KEYS
)
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ACRONYM_CASE_BOUNDARY = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_KEY_CHARACTER = re.compile(r"[^A-Za-z0-9]+")
_EMAIL_CANARY = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])"
)
_PAYMENT_CARD_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_OPAQUE_GENERATED_ID = re.compile(
    r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"|[0-7][0-9A-HJKMNP-TV-Z]{25})"
)
_FORBIDDEN_LLM_KEY_SEQUENCES = (
    "access_token",
    "api_key",
    "api_keys",
    "api_token",
    "client_secret",
    "connection_string",
    "database_handle",
    "db_handle",
    "private_key",
    "refresh_token",
    "source_handle",
    "tool_calls",
    "tool_choice",
)
_SEMANTIC_REPORT_STATUSES = frozenset(
    {
        PipelineStatus.COMPLETED,
        PipelineStatus.COMPLETED_WITH_WARNINGS,
        PipelineStatus.NEEDS_REVIEW,
        PipelineStatus.REJECTED_SECURITY,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    }
)
_VALIDATION_REPORT_STATUSES = _SEMANTIC_REPORT_STATUSES
_LOAD_REPORT_STATUSES = frozenset(
    {
        PipelineStatus.COMPLETED,
        PipelineStatus.COMPLETED_WITH_WARNINGS,
        PipelineStatus.ROLLED_BACK,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    }
)
_SECURITY_REPORT_STATUSES = frozenset(
    {
        PipelineStatus.COMPLETED,
        PipelineStatus.COMPLETED_WITH_WARNINGS,
        PipelineStatus.REJECTED_SECURITY,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    }
)


def _reject_json_constant(value: str) -> Never:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("canonical JSON cannot contain duplicate object keys")
        result[key] = value
    return result


def _validate_json_nesting(value: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > 64:
                raise ValueError("structured payload exceeds the nesting limit")
        elif character in "]}":
            depth -= 1
            if depth < 0:
                raise ValueError("structured payload has invalid nesting")


def _validate_canonical_json_object(value: str) -> str:
    _validate_json_nesting(value)
    parsed: object = json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(parsed, dict):
        raise ValueError("structured payload must be a JSON object")
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if canonical != value:
        raise ValueError("structured payload must use canonical JSON encoding")
    if len(value.encode("utf-8")) > 1_048_576:
        raise ValueError("structured payload exceeds the byte limit")
    return value


def _validate_llm_json_object(value: str) -> str:
    canonical = _validate_canonical_json_object(value)
    parsed = json.loads(canonical)
    pending: list[object] = [parsed]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, nested in current.items():
                if any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    for character in key
                ):
                    raise ValueError(
                        "LLM payload key contains control/format characters"
                    )
                normalized_key = (
                    _NON_KEY_CHARACTER.sub(
                        "_",
                        _CAMEL_CASE_BOUNDARY.sub(
                            "_",
                            _ACRONYM_CASE_BOUNDARY.sub("_", key),
                        ),
                    )
                    .strip("_")
                    .casefold()
                )
                key_parts = frozenset(normalized_key.split("_"))
                compact_key = normalized_key.replace("_", "")
                padded_key = f"_{normalized_key}_"
                contains_forbidden_sequence = any(
                    f"_{sequence}_" in padded_key
                    or sequence.replace("_", "") in compact_key
                    for sequence in _FORBIDDEN_LLM_KEY_SEQUENCES
                )
                if (
                    normalized_key in _FORBIDDEN_LLM_KEYS
                    or key_parts & _FORBIDDEN_LLM_KEY_PARTS
                    or compact_key in _FORBIDDEN_LLM_COMPACT_KEYS
                    or contains_forbidden_sequence
                ):
                    raise ValueError("LLM payload contains a forbidden key")
                pending.append(nested)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str) and _contains_credential_canary(current):
            raise ValueError("LLM payload contains a credential/DSN canary")
    return canonical


def _json_fingerprint(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _validate_unique_fingerprints(values: tuple[str, ...]) -> None:
    if len(set(values)) != len(values):
        raise ValueError("artifact fingerprints must be unique")


def _has_error_issue(issues: tuple[ValidationIssue, ...]) -> bool:
    return any(
        issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
        for issue in issues
    )


def _has_warning_issue(issues: tuple[ValidationIssue, ...]) -> bool:
    return any(issue.severity is IssueSeverity.WARNING for issue in issues)


def _reject_unbound_physical_issue_refs(
    issues: tuple[ValidationIssue, ...],
) -> None:
    if any(issue.source_refs for issue in issues):
        raise ValueError(
            "stage report issues cannot contain unbound physical source_refs"
        )


def _passes_luhn(candidate: str) -> bool:
    digits = [
        int(character)
        for character in candidate
        if character.isascii() and character.isdigit()
    ]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


def _reject_sensitive_audit_identifier(value: str) -> str:
    if _EMAIL_CANARY.search(value):
        raise ValueError("Audit identifier не должен содержать email")
    if any(
        _passes_luhn(match.group()) for match in _PAYMENT_CARD_CANDIDATE.finditer(value)
    ):
        raise ValueError("Audit identifier не должен содержать номер платёжной карты")
    return value


def _reject_nonopaque_event_id(value: str) -> str:
    prefix = "event-"
    if not value.startswith(prefix) or not _OPAQUE_GENERATED_ID.fullmatch(
        value.removeprefix(prefix)
    ):
        raise ValueError("Audit event ID должен иметь вид event-<UUID|ULID>")
    return value


def _reject_nonopaque_run_id(value: str) -> str:
    prefix = "run-"
    if not value.startswith(prefix) or not _OPAQUE_GENERATED_ID.fullmatch(
        value.removeprefix(prefix)
    ):
        raise ValueError("Audit run ID должен иметь вид run-<UUID|ULID>")
    return value


_AuditIdentifier = Annotated[
    IdentifierStr, AfterValidator(_reject_sensitive_audit_identifier)
]
_OpaqueAuditEventId = Annotated[
    _AuditIdentifier, AfterValidator(_reject_nonopaque_event_id)
]
_OpaqueAuditRunId = Annotated[
    _AuditIdentifier, AfterValidator(_reject_nonopaque_run_id)
]


class SemanticParseReport(FrozenContract):
    """Итог применения проверенного ParsePlan к extracted dataset."""

    schema_version: SchemaVersionStr = "1.0.0"
    run_id: IdentifierStr
    producer: ProducerMetadata
    status: PipelineStatus
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr | None = None
    records: NonNegativeInt = 0
    unresolved_blocks: NonNegativeInt = 0
    provenance_coverage: ConfidenceDecimal
    llm_calls: NonNegativeInt = 0
    issues: tuple[ValidationIssue, ...] = ()
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_terminal_fingerprint(self) -> Self:
        _reject_unbound_physical_issue_refs(self.issues)
        if self.status not in _SEMANTIC_REPORT_STATUSES:
            raise ValueError("status не относится к semantic parse report")
        completed = self.status in {
            PipelineStatus.COMPLETED,
            PipelineStatus.COMPLETED_WITH_WARNINGS,
        }
        if completed and self.normalized_fingerprint is None:
            raise ValueError("completed semantic parse requires normalized fingerprint")
        if not completed and self.normalized_fingerprint is not None:
            raise ValueError(
                "incomplete semantic parse cannot claim normalized artifact"
            )
        if completed and _has_error_issue(self.issues):
            raise ValueError("completed semantic parse cannot contain error issues")
        warning_evidence = (
            self.unresolved_blocks > 0
            or self.provenance_coverage < 1
            or _has_warning_issue(self.issues)
        )
        if self.status is PipelineStatus.COMPLETED and warning_evidence:
            raise ValueError("COMPLETED semantic parse cannot contain warnings")
        if (
            self.status is PipelineStatus.COMPLETED_WITH_WARNINGS
            and not warning_evidence
        ):
            raise ValueError("COMPLETED_WITH_WARNINGS requires warning evidence")
        if not completed and not self.issues:
            raise ValueError("incomplete semantic parse requires an issue")
        return self


class ValidationReport(FrozenContract):
    """Aggregated validation outcome с безопасными typed issues."""

    schema_version: SchemaVersionStr = "1.0.0"
    run_id: IdentifierStr
    producer: ProducerMetadata
    status: PipelineStatus
    decision: ValidationDecision
    artifact_fingerprints: tuple[FingerprintStr, ...]
    total_records: NonNegativeInt = 0
    valid_records: NonNegativeInt = 0
    invalid_records: NonNegativeInt = 0
    issues: tuple[ValidationIssue, ...] = ()
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_counts_and_decision(self) -> Self:
        _reject_unbound_physical_issue_refs(self.issues)
        if self.status not in _VALIDATION_REPORT_STATUSES:
            raise ValueError("status не относится к validation report")
        _validate_unique_fingerprints(self.artifact_fingerprints)
        if not self.artifact_fingerprints:
            raise ValueError("validation report requires artifact provenance")
        if self.valid_records + self.invalid_records > self.total_records:
            raise ValueError("validation record counts exceed total records")
        if self.decision is ValidationDecision.ACCEPTED and self.invalid_records:
            raise ValueError("accepted validation cannot contain invalid records")
        if (
            self.decision is ValidationDecision.ACCEPTED
            and self.status
            in {PipelineStatus.COMPLETED, PipelineStatus.COMPLETED_WITH_WARNINGS}
            and self.valid_records + self.invalid_records != self.total_records
        ):
            raise ValueError("completed validation must account for every record")
        if self.decision is ValidationDecision.ACCEPTED and any(
            issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
            for issue in self.issues
        ):
            raise ValueError("accepted validation cannot contain error issues")
        if self.decision is ValidationDecision.ACCEPTED and self.status not in {
            PipelineStatus.COMPLETED,
            PipelineStatus.COMPLETED_WITH_WARNINGS,
        }:
            raise ValueError("accepted validation requires completed status")
        if (
            self.decision is ValidationDecision.ACCEPTED
            and self.status is PipelineStatus.COMPLETED
            and _has_warning_issue(self.issues)
        ):
            raise ValueError("COMPLETED validation cannot contain warnings")
        if (
            self.decision is ValidationDecision.ACCEPTED
            and self.status is PipelineStatus.COMPLETED_WITH_WARNINGS
            and not _has_warning_issue(self.issues)
        ):
            raise ValueError("COMPLETED_WITH_WARNINGS requires warning evidence")
        if (
            self.decision is ValidationDecision.NEEDS_REVIEW
            and self.status is not PipelineStatus.NEEDS_REVIEW
        ):
            raise ValueError("needs_review decision requires NEEDS_REVIEW status")
        if self.decision is ValidationDecision.REJECTED and self.status not in {
            PipelineStatus.FAILED,
            PipelineStatus.REJECTED_SECURITY,
            PipelineStatus.CANCELLED,
        }:
            raise ValueError(
                "rejected validation requires failed/security/cancelled status"
            )
        if self.decision is not ValidationDecision.ACCEPTED and not self.issues:
            raise ValueError("non-accepted validation requires an issue")
        return self


class LoadReport(FrozenContract):
    """Подтверждённый outcome dry-run либо транзакционной загрузки."""

    schema_version: SchemaVersionStr = "1.0.0"
    run_id: IdentifierStr
    producer: ProducerMetadata
    status: PipelineStatus
    operation: LoadOperation
    transaction_outcome: TransactionOutcome
    dry_run: StrictBool
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    mapping_plan_fingerprint: FingerprintStr
    mapping_validation_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    artifact_fingerprints: tuple[FingerprintStr, ...]
    attempted_records: NonNegativeInt = 0
    loaded_records: NonNegativeInt = 0
    would_load_records: NonNegativeInt = 0
    rejected_records: NonNegativeInt = 0
    issues: tuple[ValidationIssue, ...] = ()
    generated_at: UtcDateTime

    @model_validator(mode="after")
    def validate_transaction_outcome(self) -> Self:
        _reject_unbound_physical_issue_refs(self.issues)
        if self.status not in _LOAD_REPORT_STATUSES:
            raise ValueError("status не относится к load report")
        _validate_unique_fingerprints(self.artifact_fingerprints)
        if not self.artifact_fingerprints:
            raise ValueError("load report requires artifact provenance")
        execution_fingerprints = {
            self.source_fingerprint,
            self.extraction_fingerprint,
            self.parse_plan_fingerprint,
            self.normalized_fingerprint,
            self.mapping_plan_fingerprint,
            self.mapping_validation_fingerprint,
            self.database_fingerprint,
            self.target_policy_fingerprint,
        }
        if not execution_fingerprints.issubset(self.artifact_fingerprints):
            raise ValueError("load report artifact provenance is incomplete")
        processed_records = (
            self.would_load_records if self.dry_run else self.loaded_records
        )
        if processed_records + self.rejected_records > self.attempted_records:
            raise ValueError("load record counts exceed attempted records")
        completed = self.status in {
            PipelineStatus.COMPLETED,
            PipelineStatus.COMPLETED_WITH_WARNINGS,
        }
        if completed and _has_error_issue(self.issues):
            raise ValueError("completed load cannot contain error issues")
        if (
            completed
            and not self.dry_run
            and self.loaded_records + self.rejected_records != self.attempted_records
        ):
            raise ValueError("completed load must account for every attempted record")
        if (
            completed
            and self.dry_run
            and self.would_load_records + self.rejected_records
            != self.attempted_records
        ):
            raise ValueError(
                "completed dry-run must account for every attempted record"
            )
        warning_evidence = self.rejected_records > 0 or _has_warning_issue(self.issues)
        if self.status is PipelineStatus.COMPLETED and warning_evidence:
            raise ValueError("COMPLETED load cannot contain warnings/rejections")
        if (
            self.status is PipelineStatus.COMPLETED_WITH_WARNINGS
            and not warning_evidence
        ):
            raise ValueError("COMPLETED_WITH_WARNINGS requires warning evidence")
        if not completed and not self.issues:
            raise ValueError("incomplete load requires an issue")
        if self.dry_run:
            if self.loaded_records:
                raise ValueError("dry-run report cannot contain loaded records")
            if completed and self.transaction_outcome is not TransactionOutcome.DRY_RUN:
                raise ValueError("completed dry-run requires dry_run outcome")
            if not completed and self.transaction_outcome not in {
                TransactionOutcome.NOT_STARTED,
                TransactionOutcome.UNKNOWN,
            }:
                raise ValueError("incomplete dry-run requires unconfirmed outcome")
        else:
            if self.would_load_records:
                raise ValueError("write report cannot contain would-load records")
            if self.transaction_outcome is TransactionOutcome.DRY_RUN:
                raise ValueError("write report cannot use dry_run transaction outcome")
        if (
            not self.dry_run
            and self.status
            in {PipelineStatus.COMPLETED, PipelineStatus.COMPLETED_WITH_WARNINGS}
            and self.transaction_outcome is not TransactionOutcome.COMMITTED
        ):
            raise ValueError("completed write requires committed transaction outcome")
        if (
            self.loaded_records
            and self.transaction_outcome is not TransactionOutcome.COMMITTED
        ):
            raise ValueError("loaded records require committed transaction evidence")
        if (
            self.transaction_outcome is TransactionOutcome.ROLLED_BACK
            and self.loaded_records
        ):
            raise ValueError("rolled-back transaction cannot contain loaded records")
        if (
            self.status is PipelineStatus.ROLLED_BACK
            and self.transaction_outcome is not TransactionOutcome.ROLLED_BACK
        ):
            raise ValueError("ROLLED_BACK status requires rollback evidence")
        if (
            self.transaction_outcome is TransactionOutcome.COMMITTED
            and self.status
            not in {PipelineStatus.COMPLETED, PipelineStatus.COMPLETED_WITH_WARNINGS}
        ):
            raise ValueError("committed transaction requires completed status")
        if self.transaction_outcome in {
            TransactionOutcome.NOT_STARTED,
            TransactionOutcome.UNKNOWN,
        } and self.status not in {PipelineStatus.FAILED, PipelineStatus.CANCELLED}:
            raise ValueError("unconfirmed transaction requires failed/cancelled status")
        return self


class SecurityReport(FrozenContract):
    """Итог security checks без raw payload и secret-bearing details."""

    schema_version: SchemaVersionStr = "1.0.0"
    request_id: IdentifierStr
    run_id: IdentifierStr
    purpose: _SecurityPurpose
    content_fingerprint: FingerprintStr
    payload_fingerprint: FingerprintStr
    data_classification: DataClassification
    routing_policy_id: IdentifierStr
    routing_policy_fingerprint: FingerprintStr
    redaction_fingerprint: FingerprintStr
    producer: ProducerMetadata
    decision: Literal["allowed", "blocked", "error"]
    status: PipelineStatus
    artifact_fingerprints: tuple[FingerprintStr, ...]
    scanned_items: NonNegativeInt = 0
    blocked_items: NonNegativeInt = 0
    prompt_injection_detected: StrictBool = False
    issues: tuple[ValidationIssue, ...] = ()
    generated_at: UtcDateTime

    _exact_classification = field_validator("data_classification", mode="before")(
        _exact_data_classification_input
    )

    @model_validator(mode="after")
    def validate_security_counts(self) -> Self:
        _reject_unbound_physical_issue_refs(self.issues)
        if self.status not in _SECURITY_REPORT_STATUSES:
            raise ValueError("status не относится к security report")
        _validate_unique_fingerprints(self.artifact_fingerprints)
        if not self.artifact_fingerprints:
            raise ValueError("security report требует artifact provenance")
        if self.content_fingerprint not in self.artifact_fingerprints:
            raise ValueError("content fingerprint отсутствует в artifact provenance")
        if self.payload_fingerprint not in self.artifact_fingerprints:
            raise ValueError("payload fingerprint отсутствует в artifact provenance")
        if self.blocked_items > self.scanned_items:
            raise ValueError("blocked item count exceeds scanned item count")
        if self.decision == "allowed":
            if not self.scanned_items:
                raise ValueError("allowed security report требует фактического scan")
            if self.blocked_items or self.prompt_injection_detected:
                raise ValueError("allowed security report не может блокировать items")
            if _has_error_issue(self.issues):
                raise ValueError("allowed security report не может содержать errors")
            if self.status not in {
                PipelineStatus.COMPLETED,
                PipelineStatus.COMPLETED_WITH_WARNINGS,
            }:
                raise ValueError("allowed security report требует completed status")
            if self.status is PipelineStatus.COMPLETED and _has_warning_issue(
                self.issues
            ):
                raise ValueError("COMPLETED security report cannot contain warnings")
            if (
                self.status is PipelineStatus.COMPLETED_WITH_WARNINGS
                and not _has_warning_issue(self.issues)
            ):
                raise ValueError("COMPLETED_WITH_WARNINGS requires warning evidence")
        elif self.decision == "blocked":
            if not self.blocked_items or not self.issues:
                raise ValueError("blocked security report требует blocked items/issues")
            if self.status is not PipelineStatus.REJECTED_SECURITY:
                raise ValueError("blocked decision требует REJECTED_SECURITY status")
        else:
            if self.status not in {PipelineStatus.FAILED, PipelineStatus.CANCELLED}:
                raise ValueError("security error требует failed/cancelled status")
            if not self.issues:
                raise ValueError("security error требует issues")
        return self


class SecurityApproval(FrozenContract):
    """Проверяемое свидетельство разрешающего security report."""

    report: SecurityReport
    report_fingerprint: FingerprintStr

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.report.decision != "allowed":
            raise ValueError("security approval requires allowed report")
        if self.report_fingerprint != _json_fingerprint(self.report.canonical_json()):
            raise ValueError("security report fingerprint mismatch")
        return self


class AuditEvent(FrozenContract):
    """Append-only событие с artifact fingerprints и без произвольного payload."""

    schema_version: SchemaVersionStr = "1.0.0"
    event_id: _OpaqueAuditEventId
    run_id: _OpaqueAuditRunId
    occurred_at: UtcDateTime
    status: PipelineStatus
    event_type: _AuditIdentifier
    artifact_fingerprints: tuple[FingerprintStr, ...]
    producer: ProducerMetadata
    redaction_fingerprint: FingerprintStr | None = None
    security_report: SecurityReport | None = None
    security_report_fingerprint: FingerprintStr | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> Self:
        _validate_unique_fingerprints(self.artifact_fingerprints)
        if not self.artifact_fingerprints:
            raise ValueError("audit event requires artifact provenance")
        producer_identifiers = (
            self.producer.component_id,
            self.producer.provider_id,
            self.producer.model_id,
        )
        for identifier in producer_identifiers:
            if identifier is not None:
                _reject_sensitive_audit_identifier(identifier)
        evidence = (
            self.redaction_fingerprint,
            self.security_report,
            self.security_report_fingerprint,
        )
        present_evidence = tuple(item is not None for item in evidence)
        if any(present_evidence) and not all(present_evidence):
            raise ValueError("audit security evidence указывается полным набором")
        pre_security = self.status in {
            PipelineStatus.CREATED,
            PipelineStatus.SOURCE_PROBING,
        }
        terminal_security = self.status in {
            PipelineStatus.COMPLETED,
            PipelineStatus.COMPLETED_WITH_WARNINGS,
            PipelineStatus.REJECTED_SECURITY,
        }
        if pre_security and any(present_evidence):
            raise ValueError("pre-security audit event не принимает security evidence")
        if terminal_security and not all(present_evidence):
            raise ValueError("terminal audit event требует security evidence")
        if self.security_report is None or self.security_report_fingerprint is None:
            return self
        if self.security_report.run_id != self.run_id:
            raise ValueError("audit/security report run identity mismatch")
        if self.security_report.generated_at > self.occurred_at:
            raise ValueError("audit event cannot precede its security report")
        if self.redaction_fingerprint != self.security_report.redaction_fingerprint:
            raise ValueError("audit/security report redaction mismatch")
        if self.security_report_fingerprint != _json_fingerprint(
            self.security_report.canonical_json()
        ):
            raise ValueError("audit security report fingerprint mismatch")
        if self.security_report_fingerprint not in self.artifact_fingerprints:
            raise ValueError("audit provenance не содержит security report")
        if (
            self.status
            in {PipelineStatus.COMPLETED, PipelineStatus.COMPLETED_WITH_WARNINGS}
            and self.security_report.decision != "allowed"
        ):
            raise ValueError("successful audit event требует allowed security report")
        if (
            self.status is PipelineStatus.REJECTED_SECURITY
            and self.security_report.decision != "blocked"
        ):
            raise ValueError("REJECTED_SECURITY audit event требует blocked report")
        if (
            self.security_report.decision == "blocked"
            and self.status is not PipelineStatus.REJECTED_SECURITY
        ):
            raise ValueError("blocked security report требует REJECTED_SECURITY event")
        if self.security_report.decision == "error" and self.status not in {
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        }:
            raise ValueError("security error report требует FAILED/CANCELLED event")
        if (
            self.status is PipelineStatus.COMPLETED
            and self.security_report.status is not PipelineStatus.COMPLETED
        ):
            raise ValueError("COMPLETED audit event cannot hide security warnings")
        return self


class SecurityScanRequest(FrozenContract):
    """Bounded scan input без filesystem/DB handles."""

    request_id: IdentifierStr
    run_id: IdentifierStr
    purpose: _SecurityPurpose
    content_fingerprint: FingerprintStr
    payload_json: _CanonicalJson
    payload_fingerprint: FingerprintStr
    data_classification: DataClassification
    routing_policy_id: IdentifierStr
    routing_policy_fingerprint: FingerprintStr
    redaction_fingerprint: FingerprintStr

    _exact_classification = field_validator("data_classification", mode="before")(
        _exact_data_classification_input
    )
    _canonical_payload = field_validator("payload_json")(
        _validate_canonical_json_object
    )

    @model_validator(mode="after")
    def validate_payload_fingerprint(self) -> Self:
        if self.payload_fingerprint != _json_fingerprint(self.payload_json):
            raise ValueError(
                "payload fingerprint does not match canonical scan payload"
            )
        return self


class ProviderCapabilities(FrozenContract):
    """Immutable capabilities конкретной LLM-конфигурации."""

    provider_id: IdentifierStr
    provider_version: VersionStr
    model_id: IdentifierStr
    structured_output: StrictBool
    supported_purposes: tuple[_LlmPurpose, ...]
    max_input_bytes: _PayloadByteLimit
    max_output_bytes: _PayloadByteLimit

    @model_validator(mode="after")
    def validate_purposes(self) -> Self:
        if not self.structured_output:
            raise ValueError("LLM provider must support structured output")
        if not self.supported_purposes:
            raise ValueError("LLM provider must declare at least one purpose")
        if len(set(self.supported_purposes)) != len(self.supported_purposes):
            raise ValueError("LLM provider contains duplicate purposes")
        return self


class LLMRequest(FrozenContract):
    """Minimized structured request без tools, credentials и handles."""

    request_id: IdentifierStr
    run_id: IdentifierStr
    purpose: _LlmPurpose
    response_schema_id: IdentifierStr
    response_schema_version: VersionStr
    payload_json: _CanonicalJson
    payload_fingerprint: FingerprintStr
    content_fingerprint: FingerprintStr
    data_classification: DataClassification
    routing_policy_id: IdentifierStr
    routing_policy_fingerprint: FingerprintStr
    redaction_fingerprint: FingerprintStr
    security_approval: SecurityApproval
    prompt_fingerprint: FingerprintStr
    max_output_bytes: _PayloadByteLimit

    _exact_classification = field_validator("data_classification", mode="before")(
        _exact_data_classification_input
    )
    _canonical_payload = field_validator("payload_json")(_validate_llm_json_object)

    @model_validator(mode="after")
    def validate_payload_fingerprint(self) -> Self:
        if self.payload_fingerprint != _json_fingerprint(self.payload_json):
            raise ValueError("payload fingerprint does not match canonical JSON")
        security_report = self.security_approval.report
        if security_report.run_id != self.run_id:
            raise ValueError("security approval run identity mismatch")
        if security_report.purpose != "llm_input":
            raise ValueError("LLM request requires llm_input security approval")
        if security_report.content_fingerprint != self.content_fingerprint:
            raise ValueError("security approval content fingerprint mismatch")
        if security_report.payload_fingerprint != self.payload_fingerprint:
            raise ValueError("security approval payload fingerprint mismatch")
        if security_report.data_classification != self.data_classification:
            raise ValueError("security approval data classification mismatch")
        if security_report.routing_policy_id != self.routing_policy_id:
            raise ValueError("security approval routing policy mismatch")
        if (
            security_report.routing_policy_fingerprint
            != self.routing_policy_fingerprint
        ):
            raise ValueError("security approval routing fingerprint mismatch")
        if security_report.redaction_fingerprint != self.redaction_fingerprint:
            raise ValueError("security approval redaction fingerprint mismatch")
        return self


class LLMResponse(FrozenContract):
    """Bounded structured output с identity фактического provider/model."""

    request_id: IdentifierStr
    provider_id: IdentifierStr
    provider_version: VersionStr
    model_id: IdentifierStr
    response_schema_id: IdentifierStr
    response_schema_version: VersionStr
    output_json: _CanonicalJson
    prompt_fingerprint: FingerprintStr
    generation_fingerprint: FingerprintStr
    finish_reason: IdentifierStr
    input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt
    generated_at: UtcDateTime

    _canonical_output = field_validator("output_json")(_validate_llm_json_object)

    @model_validator(mode="after")
    def validate_output_fingerprint(self) -> Self:
        if self.generation_fingerprint != _json_fingerprint(self.output_json):
            raise ValueError("generation fingerprint does not match canonical output")
        return self
