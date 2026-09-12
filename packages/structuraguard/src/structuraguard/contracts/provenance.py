"""Provenance evidence и итоговый report без копирования restricted scalars."""

from collections.abc import Iterator
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StrictStr, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import (
    FingerprintStr,
    IdentifierStr,
    IssueCodeStr,
    IssueSeverity,
    NonNegativeInt,
    PhysicalSourceRef,
    SourceArtifactRef,
)
from .document_semantics import SourceTextSpan
from .normalization import NormalizationPolicy, NormalizerSpec
from .normalized import SemanticFieldRef
from .reports import ValidationReport
from .source import SourceLocation


class _Sensitive(FrozenContract):
    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())


class ValidationLayer(StrEnum):
    """Порядок отчёта; provenance replay выполняется до вызова normalizers."""

    NORMALIZATION = "normalization"
    JSON_SCHEMA = "json_schema"
    DATABASE = "database"
    BUSINESS_RULES = "business_rules"
    PROVENANCE = "provenance"


class ProvenanceLimits(FrozenContract):
    """Лимиты завершённого snapshot и retained evidence, до serialization/replay."""

    max_batches: Annotated[StrictInt, Field(ge=1, le=10_000)] = 1_000
    max_records: Annotated[StrictInt, Field(ge=1, le=100_000)] = 10_000
    max_values: Annotated[StrictInt, Field(ge=1, le=250_000)] = 50_000
    max_issues: Annotated[StrictInt, Field(ge=1, le=100_000)] = 10_000
    max_nodes: Annotated[StrictInt, Field(ge=1, le=2_000_000)] = 500_000
    max_bytes: Annotated[StrictInt, Field(ge=1, le=134_217_728)] = 33_554_432
    max_depth: Annotated[StrictInt, Field(ge=1, le=64)] = 48
    max_scalar_bytes: Annotated[StrictInt, Field(ge=1, le=262_144)] = 65_536
    max_numeric_digits: Annotated[StrictInt, Field(ge=1, le=1_024)] = 128
    max_decimal_exponent: Annotated[StrictInt, Field(ge=1, le=10_000)] = 1_024


class NormalizationBinding(_Sensitive):
    """Разрешённая владельцем цепочка для semantic field, включая locale policy."""

    field: SemanticFieldRef
    steps: Annotated[tuple[NormalizerSpec, ...], Field(min_length=1, max_length=64)]
    policy: NormalizationPolicy = NormalizationPolicy()


class ProvenancePolicy(_Sensitive):
    """Ненулевые значения требуют origins по умолчанию; null не освобождён от replay."""

    require_non_null: StrictBool = True
    require_null: StrictBool = False
    normalizations: Annotated[
        tuple[NormalizationBinding, ...], Field(max_length=256)
    ] = ()
    limits: ProvenanceLimits = ProvenanceLimits()

    @model_validator(mode="after")
    def _unique_bindings(self) -> Self:
        if len({binding.field for binding in self.normalizations}) != len(
            self.normalizations
        ):
            raise ValueError("Normalization bindings должны иметь уникальные fields")
        return self


class ValidationFinding(_Sensitive):
    """Один check/location/code; ordinals относятся к полному normalized snapshot."""

    layer: ValidationLayer
    code: IssueCodeStr
    severity: IssueSeverity = IssueSeverity.ERROR
    outcome: Literal["failed", "unverified", "warning", "info"] = "failed"
    record_index: NonNegativeInt | None = None
    entity_index: NonNegativeInt | None = None
    value_index: NonNegativeInt | None = None
    check_index: NonNegativeInt = 0
    json_path: Annotated[StrictStr, Field(min_length=1, max_length=4096)] | None = None

    @model_validator(mode="after")
    def _scope(self) -> Self:
        if self.entity_index is not None and self.record_index is None:
            raise ValueError("Entity ordinal требует record ordinal")
        if self.value_index is not None and self.entity_index is None:
            raise ValueError("Value ordinal требует entity ordinal")
        expected = {
            "failed": (IssueSeverity.ERROR, IssueSeverity.CRITICAL),
            "unverified": (IssueSeverity.WARNING,),
            "warning": (IssueSeverity.WARNING,),
            "info": (IssueSeverity.INFO,),
        }
        if self.severity not in expected[self.outcome]:
            raise ValueError("Finding outcome не соответствует severity")
        return self

    def ordering_key(self) -> tuple[int, int, int, int, int, str, str, str, str]:
        """Полный deterministic tie-break без зависимости от порядка callbacks."""
        return (
            tuple(ValidationLayer).index(self.layer),
            -1 if self.record_index is None else self.record_index,
            -1 if self.entity_index is None else self.entity_index,
            -1 if self.value_index is None else self.value_index,
            self.check_index,
            self.code,
            self.json_path or "",
            self.outcome,
            self.severity.value,
        )


def _finding_counts(
    findings: tuple[ValidationFinding, ...], total: int
) -> tuple[int, int, int, int]:
    """Глобальный finding учитывается за O(1), без repeated expansion records."""
    invalid: set[int] = set()
    unresolved: set[int] = set()
    warnings: set[int] = set()
    all_invalid = all_unresolved = all_warnings = False
    for finding in findings:
        index = finding.record_index
        if index is not None and index >= total:
            raise ValueError("Finding содержит неизвестный record ordinal")
        if finding.outcome == "failed":
            if index is None:
                all_invalid = True
            else:
                invalid.add(index)
        elif finding.outcome == "unverified":
            if index is None:
                all_unresolved = True
            else:
                unresolved.add(index)
        if finding.severity is IssueSeverity.WARNING:
            if index is None:
                all_warnings = True
            else:
                warnings.add(index)
    invalid_count = total if all_invalid else len(invalid)
    unresolved_count = (
        (total - invalid_count if all_unresolved else len(unresolved - invalid))
        if not all_invalid
        else 0
    )
    return (
        total - invalid_count - unresolved_count,
        invalid_count,
        unresolved_count,
        total if all_warnings else len(warnings),
    )


class ArtifactValueReference(_Sensitive):
    """Ссылка на immutable artifact; pointer не открывается и не исполняется."""

    artifact_fingerprint: FingerprintStr
    pointer: Annotated[StrictStr, Field(min_length=1, max_length=4096)]


class SourceEvidenceLocation(_Sensitive):
    """Заявленные physical ref/location/spans; DTO не открывает location и не доказывает его."""

    source_ref: PhysicalSourceRef
    location: SourceLocation
    source_spans: Annotated[tuple[SourceTextSpan, ...], Field(max_length=8)] = ()


class ValueProvenanceEvidence(_Sensitive):
    """Ссылки на raw/normalized и заявленные locations; verified только после replay."""

    record_index: NonNegativeInt
    entity_index: NonNegativeInt
    value_index: NonNegativeInt
    value_id: IdentifierStr
    raw_reference: ArtifactValueReference
    normalized_reference: ArtifactValueReference
    locations: Annotated[tuple[SourceEvidenceLocation, ...], Field(max_length=64)]
    required: StrictBool
    verified: StrictBool


class ValidationLineage(_Sensitive):
    """Связать source, selection, normalization и policy hashes без проверки подлинности."""

    source: SourceArtifactRef
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    registry_fingerprint: FingerprintStr | None = None
    normalization_fingerprints: tuple[FingerprintStr, ...] = ()

    @property
    def input_fingerprint(self) -> str:
        """Identity точного selection + normalization sidecar и policy."""
        return canonical_sha256_value(self)


class ValidationLayerResult(_Sensitive):
    """Trusted composition передаёт проверенные результаты для точного input.

    DTO не является capability: владелец адаптирует schema/DB/rules results после
    проверки projection и их собственных fingerprints, включая catalog/policy.
    """

    layer: ValidationLayer
    input_fingerprint: FingerprintStr
    evidence_fingerprints: Annotated[
        tuple[FingerprintStr, ...], Field(min_length=1, max_length=16)
    ]
    complete: StrictBool
    findings: Annotated[tuple[ValidationFinding, ...], Field(max_length=100_000)] = ()

    @model_validator(mode="after")
    def _layer(self) -> Self:
        if self.layer in (ValidationLayer.PROVENANCE, ValidationLayer.NORMALIZATION):
            raise ValueError("Provenance/normalization требуют physical replay")
        if any(finding.layer != self.layer for finding in self.findings):
            raise ValueError("Finding не принадлежит layer result")
        return self


class ValidationIssueCount(FrozenContract):
    """Число issues одного code/severity; безопасные code buckets выбирает safe_summary."""

    code: IssueCodeStr
    severity: IssueSeverity
    count: NonNegativeInt


class ValidationSafeSummary(FrozenContract):
    """Только закрытые статусы, числовые агрегаты и allowlisted code buckets."""

    accepted: StrictBool
    complete: StrictBool
    total_records: NonNegativeInt
    valid_records: NonNegativeInt
    invalid_records: NonNegativeInt
    unresolved_records: NonNegativeInt
    warning_records: NonNegativeInt
    required_values: NonNegativeInt
    verified_values: NonNegativeInt
    issue_counts: tuple[ValidationIssueCount, ...]


class DetailedValidationReport(ValidationReport):
    """Совместимый ValidationReport с evidence sidecar; full JSON считается sensitive."""

    schema_version: Literal["1.1.0"] = "1.1.0"
    lineage: ValidationLineage
    complete: StrictBool
    required_layers: tuple[ValidationLayer, ...]
    completed_layers: tuple[ValidationLayer, ...]
    unresolved_records: NonNegativeInt
    warning_records: NonNegativeInt
    required_values: NonNegativeInt
    verified_values: NonNegativeInt
    findings: tuple[ValidationFinding, ...]
    evidence: tuple[ValueProvenanceEvidence, ...]
    layer_results: tuple[ValidationLayerResult, ...] = ()
    evidence_fingerprint: FingerprintStr = "sha256:" + "0" * 64

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())

    @model_validator(mode="after")
    def _evidence(self) -> Self:
        from .common import ValidationDecision, ValidationIssue

        if (
            self.valid_records + self.invalid_records + self.unresolved_records
            != self.total_records
        ):
            raise ValueError("Report должен учитывать каждую record ровно один раз")
        if (
            self.warning_records > self.total_records
            or self.verified_values > self.required_values
        ):
            raise ValueError("Report coverage/counters несогласованы")
        for layers in (self.required_layers, self.completed_layers):
            if layers != tuple(layer for layer in ValidationLayer if layer in layers):
                raise ValueError("Layers должны быть уникальны и упорядочены")
        if (
            not self.required_layers
            or ValidationLayer.PROVENANCE not in self.required_layers
        ):
            raise ValueError("Report требует provenance layer")
        if self.complete != set(self.required_layers).issubset(self.completed_layers):
            raise ValueError("Complete требует все requested layers")
        if self.findings != tuple(
            sorted(set(self.findings), key=lambda item: item.ordering_key())
        ):
            raise ValueError("Findings должны быть уникальны и упорядочены")
        if self.issues != tuple(
            ValidationIssue(
                code=item.code, severity=item.severity, message_key=item.code
            )
            for item in self.findings
        ):
            raise ValueError("Summary issues не соответствуют detailed findings")
        if any(
            item.record_index is not None and item.record_index >= self.total_records
            for item in self.findings
        ):
            raise ValueError("Finding содержит неизвестный record ordinal")
        if self.decision is ValidationDecision.ACCEPTED and (
            not self.complete
            or any(item.outcome == "unverified" for item in self.findings)
            or self.verified_values != self.required_values
        ):
            raise ValueError("Accepted требует завершённые проверки без blockers")
        expected_decision = (
            ValidationDecision.REJECTED
            if any(item.outcome == "failed" for item in self.findings)
            else ValidationDecision.NEEDS_REVIEW
            if not self.complete
            or any(item.outcome == "unverified" for item in self.findings)
            else ValidationDecision.ACCEPTED
        )
        if self.decision is not expected_decision:
            raise ValueError("Decision не соответствует completed checks/findings")
        if (
            self.valid_records,
            self.invalid_records,
            self.unresolved_records,
            self.warning_records,
        ) != _finding_counts(self.findings, self.total_records):
            raise ValueError("Report counters не соответствуют findings")
        coordinates = tuple(
            (item.record_index, item.entity_index, item.value_index)
            for item in self.evidence
        )
        if coordinates != tuple(sorted(set(coordinates))):
            raise ValueError("Evidence должен иметь уникальные ordered coordinates")
        value_counts: dict[str, int] = {}
        for item in self.evidence:
            value_counts[item.value_id] = value_counts.get(item.value_id, 0) + 1
        duplicate_ids = {
            identity for identity, count in value_counts.items() if count > 1
        }
        # Ошибочный stream сохраняет claimed IDs по отдельным artifact pointers;
        # такие IDs нельзя публиковать как verified или принимать целиком.
        if duplicate_ids and (
            self.decision is ValidationDecision.ACCEPTED
            or any(
                item.verified and item.value_id in duplicate_ids
                for item in self.evidence
            )
        ):
            raise ValueError("Verified evidence требует уникальные value IDs")
        if any(item.record_index >= self.total_records for item in self.evidence):
            raise ValueError("Evidence содержит неизвестный record ordinal")
        coordinate_set = set(coordinates)
        entity_set = {(r, e) for r, e, _ in coordinates}
        for finding in self.findings:
            if (
                finding.entity_index is not None
                and (finding.record_index, finding.entity_index) not in entity_set
            ):
                raise ValueError("Finding содержит неизвестный entity ordinal")
            if (
                finding.value_index is not None
                and (finding.record_index, finding.entity_index, finding.value_index)
                not in coordinate_set
            ):
                raise ValueError("Finding содержит неизвестный value ordinal")
        if self.required_values != sum(
            item.required for item in self.evidence
        ) or self.verified_values != sum(
            item.required and item.verified for item in self.evidence
        ):
            raise ValueError("Report coverage не соответствует evidence")
        if any(
            item.input_fingerprint != self.lineage.input_fingerprint
            for item in self.layer_results
        ):
            raise ValueError("Layer result не относится к input report")
        expected = canonical_sha256_value(
            self, exclude_top_level=frozenset({"generated_at", "evidence_fingerprint"})
        )
        if self.evidence_fingerprint == "sha256:" + "0" * 64:
            object.__setattr__(self, "evidence_fingerprint", expected)
        elif self.evidence_fingerprint != expected:
            raise ValueError("Report fingerprint не соответствует evidence")
        return self

    def safe_summary(self) -> ValidationSafeSummary:
        """Вернуть сводку counters/coverage для обычных logs без изменения report.

        Paths, IDs, hashes, locations и raw/normalized values исключены.
        Произвольные plugin codes заменяются фиксированными группами.
        I/O отсутствует; метод не анонимизирует сам исходный report.
        """
        from .common import ValidationDecision

        allowed = frozenset(
            {
                "PROVENANCE_REQUIRED",
                "PROVENANCE_SOURCE_MISMATCH",
                "PROVENANCE_REF_UNKNOWN",
                "PROVENANCE_LOCATION_MISMATCH",
                "PROVENANCE_RAW_MISMATCH",
                "PROVENANCE_SELECTION_MISMATCH",
                "PROVENANCE_STRUCTURE_MISMATCH",
                "PROVENANCE_REPLAY_FAILED",
                "PROVENANCE_STREAM_INVALID",
                "NORMALIZATION_EVIDENCE_MISSING",
                "NORMALIZATION_EVIDENCE_MISMATCH",
                "NORMALIZATION_EVIDENCE_UNEXPECTED",
                "NORMALIZATION_PROVENANCE_UNVERIFIED",
                "NORMALIZATION_FAILED",
                "VALIDATION_LAYER_UNVERIFIED",
            }
        )
        counts: dict[tuple[str, IssueSeverity], int] = {}
        for issue in self.issues:
            key = (
                issue.code if issue.code in allowed else "VALIDATION_ISSUE",
                issue.severity,
            )
            counts[key] = counts.get(key, 0) + 1
        return ValidationSafeSummary(
            accepted=self.decision is ValidationDecision.ACCEPTED,
            complete=self.complete,
            total_records=self.total_records,
            valid_records=self.valid_records,
            invalid_records=self.invalid_records,
            unresolved_records=self.unresolved_records,
            warning_records=self.warning_records,
            required_values=self.required_values,
            verified_values=self.verified_values,
            issue_counts=tuple(
                ValidationIssueCount(code=key[0], severity=key[1], count=count)
                for key, count in sorted(counts.items())
            ),
        )
