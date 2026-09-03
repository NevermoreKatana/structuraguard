"""Декларативный semantic-to-database mapping без SQL."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import (
    FingerprintStr,
    FiniteDecimal,
    IdentifierStr,
    IssueSeverity,
    LoadOperation,
    ProducerMetadata,
    SchemaVersionStr,
    UtcDateTime,
    ValidationDecision,
    ValidationIssue,
    VersionStr,
)
from .database import (
    CatalogColumnRef,
    DatabaseCatalog,
    MappingPolicyRef,
)
from .normalized import (
    NormalizedDatasetManifest,
    SemanticFieldRef,
)

_PositiveInt = Annotated[StrictInt, Field(gt=0)]
_AUTO_FINGERPRINT = "sha256:" + "0" * 64


def _validate_confidence(value: Decimal) -> None:
    if value < 0 or value > 1:
        raise ValueError("confidence must be between 0 and 1")


def _reject_physical_issue_refs(issues: tuple[ValidationIssue, ...]) -> None:
    if any(issue.source_refs for issue in issues):
        raise ValueError("mapping issues cannot contain physical source_refs")


class MappingCandidate(FrozenContract):
    """Неавторизованный кандидат semantic field → catalog column."""

    candidate_id: IdentifierStr
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    producer: ProducerMetadata
    source: SemanticFieldRef
    target: CatalogColumnRef
    confidence: FiniteDecimal
    evidence: tuple[IdentifierStr, ...] = ()
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        _reject_physical_issue_refs(self.issues)
        _validate_confidence(self.confidence)
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("mapping candidate contains duplicate evidence")
        return self


class FieldMapping(FrozenContract):
    """Декларативное отображение normalized field на catalog column."""

    source: SemanticFieldRef
    target: CatalogColumnRef
    required: StrictBool = True
    confidence: FiniteDecimal = Decimal("1")

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        _validate_confidence(self.confidence)
        return self


class MappingPlan(FrozenContract):
    """Декларативно связывает normalized fields с catalog targets без SQL."""

    plan_id: IdentifierStr
    schema_version: SchemaVersionStr = "1.0.0"
    revision: _PositiveInt
    fingerprint: FingerprintStr = _AUTO_FINGERPRINT
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    mappings: tuple[FieldMapping, ...]
    operation: LoadOperation
    confidence: FiniteDecimal
    producer: ProducerMetadata

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        _validate_confidence(self.confidence)
        if not self.mappings:
            raise ValueError("mapping plan must contain at least one mapping")

        sources = tuple(mapping.source for mapping in self.mappings)
        targets = tuple(mapping.target for mapping in self.mappings)
        if len(set(sources)) != len(sources):
            raise ValueError("mapping plan contains duplicate semantic sources")
        if len(set(targets)) != len(targets):
            raise ValueError("mapping plan contains duplicate catalog targets")
        expected_fingerprint = self.content_fingerprint()
        if self.fingerprint == _AUTO_FINGERPRINT:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        elif self.fingerprint != expected_fingerprint:
            raise ValueError("mapping plan fingerprint does not match content")
        return self

    def content_fingerprint(self) -> str:
        """Вычислить canonical fingerprint plan без self-reference поля."""

        return canonical_sha256_value(
            self,
            exclude_top_level=frozenset({"fingerprint"}),
        )

    def validate_content_fingerprint(self) -> None:
        """Отклонить stale identity после изменения содержимого plan."""

        if self.fingerprint != self.content_fingerprint():
            raise ValueError("mapping plan fingerprint does not match content")


class MappingPlanValidationRequest(FrozenContract):
    """Полный immutable snapshot для независимой проверки MappingPlan."""

    plan: MappingPlan
    manifest: NormalizedDatasetManifest
    catalog: DatabaseCatalog
    policy: MappingPolicyRef

    @model_validator(mode="after")
    def validate_lineage_and_references(self) -> Self:
        plan = self.plan
        manifest = self.manifest
        catalog = self.catalog

        plan.validate_content_fingerprint()

        if plan.source_fingerprint != manifest.source.source_fingerprint:
            raise ValueError("mapping plan source fingerprint does not match manifest")
        if plan.extraction_fingerprint != manifest.extraction_fingerprint:
            raise ValueError(
                "mapping plan extraction fingerprint does not match manifest"
            )
        if plan.parse_plan_fingerprint != manifest.parse_plan_fingerprint:
            raise ValueError(
                "mapping plan parse plan fingerprint does not match manifest"
            )
        if plan.normalized_fingerprint != manifest.normalized_fingerprint:
            raise ValueError(
                "mapping plan normalized fingerprint does not match manifest"
            )
        if plan.database_fingerprint != catalog.database_fingerprint:
            raise ValueError("mapping plan database fingerprint does not match catalog")
        if plan.target_id != catalog.target_id:
            raise ValueError("mapping plan target identity does not match catalog")
        if plan.target_policy_fingerprint != catalog.target_policy_fingerprint:
            raise ValueError("mapping plan target policy does not match catalog")
        if plan.target_policy_fingerprint != self.policy.policy_fingerprint:
            raise ValueError(
                "mapping plan target policy does not match policy reference"
            )

        for mapping in plan.mappings:
            if manifest.semantic_type_for(mapping.source) is None:
                raise ValueError("mapping references an unknown semantic field")
            if not catalog.has_column(
                mapping.target.table_id,
                mapping.target.column_id,
            ):
                raise ValueError("mapping references an unknown catalog column")
        return self


class ValidatedMappingPlan(FrozenContract):
    """Хранит evidence успешной независимой проверки ``MappingPlan``.

    Evidence не является полномочием на запись и не заменяет ``LoadContext``.
    """

    plan: MappingPlan
    validator_id: IdentifierStr
    validator_version: VersionStr
    validated_at: UtcDateTime
    validation_fingerprint: FingerprintStr
    plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    decision: Literal[ValidationDecision.ACCEPTED] = ValidationDecision.ACCEPTED
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        _reject_physical_issue_refs(self.issues)
        self.plan.validate_content_fingerprint()
        if self.plan_fingerprint != self.plan.fingerprint:
            raise ValueError("validated plan fingerprint does not match plan")
        if self.normalized_fingerprint != self.plan.normalized_fingerprint:
            raise ValueError("validated normalized fingerprint does not match plan")
        if self.database_fingerprint != self.plan.database_fingerprint:
            raise ValueError("validated database fingerprint does not match plan")
        if self.target_id != self.plan.target_id:
            raise ValueError("validated target identity does not match plan")
        if self.target_policy_fingerprint != self.plan.target_policy_fingerprint:
            raise ValueError("validated target policy does not match plan")
        if any(
            issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
            for issue in self.issues
        ):
            raise ValueError("accepted mapping plan cannot contain error issues")
        return self


class MappingPlanValidationResult(FrozenContract):
    """Результат проверки, не выдающий checked wrapper при отказе."""

    validator_id: IdentifierStr
    validator_version: VersionStr
    validated_at: UtcDateTime
    validation_fingerprint: FingerprintStr
    plan_fingerprint: FingerprintStr
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    decision: ValidationDecision
    issues: tuple[ValidationIssue, ...] = ()
    validated_plan: ValidatedMappingPlan | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        _reject_physical_issue_refs(self.issues)
        if self.decision is ValidationDecision.ACCEPTED:
            if self.validated_plan is None:
                raise ValueError("accepted result requires a validated mapping plan")
            if any(
                issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
                for issue in self.issues
            ):
                raise ValueError("accepted result cannot contain error issues")
            if (
                self.validator_id,
                self.validator_version,
                self.validated_at,
                self.validation_fingerprint,
                self.plan_fingerprint,
                self.source_fingerprint,
                self.extraction_fingerprint,
                self.parse_plan_fingerprint,
                self.normalized_fingerprint,
                self.database_fingerprint,
                self.target_id,
                self.target_policy_fingerprint,
                self.issues,
            ) != (
                self.validated_plan.validator_id,
                self.validated_plan.validator_version,
                self.validated_plan.validated_at,
                self.validated_plan.validation_fingerprint,
                self.validated_plan.plan_fingerprint,
                self.validated_plan.plan.source_fingerprint,
                self.validated_plan.plan.extraction_fingerprint,
                self.validated_plan.plan.parse_plan_fingerprint,
                self.validated_plan.normalized_fingerprint,
                self.validated_plan.database_fingerprint,
                self.validated_plan.target_id,
                self.validated_plan.target_policy_fingerprint,
                self.validated_plan.issues,
            ):
                raise ValueError("validation result evidence does not match wrapper")
            return self
        if self.validated_plan is not None:
            raise ValueError("non-accepted result cannot contain a validated plan")
        if not self.issues:
            raise ValueError("non-accepted result requires issues")
        return self
