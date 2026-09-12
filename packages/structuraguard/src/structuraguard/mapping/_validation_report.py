"""Стабильные rules/codes и ограниченный accumulator без payload diagnostics."""

from dataclasses import dataclass, field

from structuraguard.contracts.common import (
    IssueSeverity,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.exceptions import ValidationError

# Позиция правила — часть versioned ordering, а не порядок обнаружения ошибки.
RULES = (
    "MAPPING_LIMIT_EXCEEDED",
    "MAPPING_PLAN_INVALID",
    "MAPPING_PLAN_VERSION_UNSUPPORTED",
    "MAPPING_SQL_FORBIDDEN",
    "MAPPING_OPERATION_FORBIDDEN",
    "MAPPING_DDL_FORBIDDEN",
    "MAPPING_PLAN_FINGERPRINT_MISMATCH",
    "MAPPING_SOURCE_LINEAGE_MISMATCH",
    "MAPPING_TARGET_MISMATCH",
    "MAPPING_POLICY_MISMATCH",
    "MAPPING_CATALOG_FINGERPRINT_MISMATCH",
    "DATABASE_METADATA_UNSUPPORTED",
    "DATABASE_SCHEMA_DRIFT",
    "MAPPING_SCHEMA_NOT_FOUND",
    "MAPPING_SOURCE_NOT_FOUND",
    "MAPPING_TABLE_NOT_FOUND",
    "MAPPING_COLUMN_NOT_FOUND",
    "MAPPING_SCHEMA_DENIED",
    "MAPPING_TABLE_DENIED",
    "MAPPING_COLUMN_DENIED",
    "MAPPING_SYSTEM_OBJECT_FORBIDDEN",
    "MAPPING_SCOPE_INVALID",
    "MAPPING_TABLE_NOT_WRITABLE",
    "MAPPING_COLUMN_NOT_WRITABLE",
    "MAPPING_GENERATED_COLUMN",
    "MAPPING_REQUIRED_TARGET_MISSING",
    "MAPPING_SOURCE_CONFLICT",
    "MAPPING_TARGET_AMBIGUOUS",
    "MAPPING_TYPE_INCOMPATIBLE",
    "MAPPING_TYPE_UNVERIFIED",
    "MAPPING_TRANSFORMATION_REQUIRED",
    "MAPPING_NULLABILITY_CONFLICT",
    "MAPPING_LENGTH_OVERFLOW",
    "MAPPING_NUMERIC_OVERFLOW",
    "MAPPING_IDENTITY_REQUIRED",
    "MAPPING_IDENTITY_AMBIGUOUS",
    "MAPPING_UPSERT_KEY_INVALID",
    "MAPPING_IDENTITY_NULLABLE",
    "MAPPING_SOURCE_IDENTITY_FORBIDDEN",
    "MAPPING_FK_NOT_FOUND",
    "MAPPING_FK_PAIR_MISMATCH",
    "MAPPING_RELATION_UNRESOLVED",
    "MAPPING_RELATION_STRATEGY_UNSUPPORTED",
    "CYCLIC_DEPENDENCY_REQUIRES_STRATEGY",
    "MAPPING_CONFIDENCE_BELOW_THRESHOLD",
)
REVIEW_CODES = frozenset({"MAPPING_TYPE_UNVERIFIED", "MAPPING_IDENTITY_AMBIGUOUS"})


def failure(code: str) -> ValidationError:
    return ValidationError(
        error_code=code, message="Проверка MappingPlan не завершена."
    )


@dataclass
class Issues:
    options: MappingValidationOptions
    entries: dict[
        tuple[int, str, int, int], tuple[ValidationIssue, MappingIssueLocation]
    ] = field(default_factory=dict)
    operations: int = 0

    def work(self, count: int = 1) -> None:
        self.operations += count
        if self.operations > self.options.max_operations:
            raise failure("MAPPING_LIMIT_EXCEEDED")

    def add(self, code: str, location: MappingIssueLocation | None = None) -> None:
        self.work()
        loc = location or MappingIssueLocation()
        key = (RULES.index(code), loc.section, loc.index, loc.component)
        if key not in self.entries and len(self.entries) >= self.options.max_issues:
            raise failure("MAPPING_LIMIT_EXCEEDED")
        self.entries[key] = (
            ValidationIssue(
                code=code,
                message_key=code,
                severity=IssueSeverity.WARNING
                if code in REVIEW_CODES
                else IssueSeverity.ERROR,
            ),
            loc,
        )

    def ordered(
        self,
    ) -> tuple[tuple[ValidationIssue, ...], tuple[MappingIssueLocation, ...]]:
        values = tuple(self.entries[k] for k in sorted(self.entries))
        return tuple(v[0] for v in values), tuple(v[1] for v in values)

    @property
    def decision(self) -> ValidationDecision:
        if any(i.severity is IssueSeverity.ERROR for i, _ in self.entries.values()):
            return ValidationDecision.REJECTED
        return (
            ValidationDecision.NEEDS_REVIEW
            if self.entries
            else ValidationDecision.ACCEPTED
        )
