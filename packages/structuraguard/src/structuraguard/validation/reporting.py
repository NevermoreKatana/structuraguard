"""Детерминированная сборка all-errors report; без raw diagnostics и I/O."""

from datetime import datetime

from structuraguard.contracts.common import (
    IssueSeverity,
    PipelineStatus,
    ProducerMetadata,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.provenance import (
    DetailedValidationReport,
    ProvenanceLimits,
    ValidationFinding,
    ValidationLayer,
    ValidationLayerResult,
    ValidationLineage,
    ValueProvenanceEvidence,
    _finding_counts,
)
from structuraguard.validation._bounded import BoundedInput, failure


def build_report(
    *,
    run_id: str,
    generated_at: datetime,
    lineage: ValidationLineage,
    total_records: int,
    findings: tuple[ValidationFinding, ...],
    evidence: tuple[ValueProvenanceEvidence, ...],
    completed_layers: tuple[ValidationLayer, ...],
    required_layers: tuple[ValidationLayer, ...],
    layer_results: tuple[ValidationLayerResult, ...] = (),
) -> DetailedValidationReport:
    ordered = tuple(sorted(set(findings), key=lambda item: item.ordering_key()))
    try:
        valid_count, invalid_count, unresolved_count, warning_count = _finding_counts(
            ordered, total_records
        )
    except ValueError:
        raise failure("VALIDATION_REPORT_SCOPE_INVALID") from None
    complete = set(required_layers).issubset(completed_layers)
    if any(item.outcome == "failed" for item in ordered):
        decision, status = ValidationDecision.REJECTED, PipelineStatus.FAILED
    elif not complete or any(item.outcome == "unverified" for item in ordered):
        decision, status = ValidationDecision.NEEDS_REVIEW, PipelineStatus.NEEDS_REVIEW
    else:
        decision = ValidationDecision.ACCEPTED
        status = (
            PipelineStatus.COMPLETED_WITH_WARNINGS
            if any(item.severity is IssueSeverity.WARNING for item in ordered)
            else PipelineStatus.COMPLETED
        )
    artifacts = tuple(
        dict.fromkeys(
            (
                lineage.source.source_fingerprint,
                lineage.extraction_fingerprint,
                lineage.parse_plan_fingerprint,
                lineage.normalized_fingerprint,
                lineage.policy_fingerprint,
                *(
                    (lineage.registry_fingerprint,)
                    if lineage.registry_fingerprint
                    else ()
                ),
                *lineage.normalization_fingerprints,
                *(
                    fp
                    for result in layer_results
                    for fp in result.evidence_fingerprints
                ),
            )
        )
    )
    return DetailedValidationReport(
        run_id=run_id,
        producer=ProducerMetadata(
            component_id="validation_engine",
            component_version="1.0.0",
            sdk_version="0.3.0",
        ),
        generated_at=generated_at,
        status=status,
        decision=decision,
        artifact_fingerprints=artifacts,
        total_records=total_records,
        valid_records=valid_count,
        invalid_records=invalid_count,
        unresolved_records=unresolved_count,
        warning_records=warning_count,
        issues=tuple(
            ValidationIssue(
                code=item.code, severity=item.severity, message_key=item.code
            )
            for item in ordered
        ),
        lineage=lineage,
        complete=complete,
        required_layers=required_layers,
        completed_layers=completed_layers,
        findings=ordered,
        evidence=evidence,
        required_values=sum(item.required for item in evidence),
        verified_values=sum(item.required and item.verified for item in evidence),
        layer_results=layer_results,
    )


class ValidationReportBuilder:
    """Объединить replay report и результаты доверенного composition owner.

    Args:
        required_layers: Обязательные уровни; PROVENANCE всегда включён.
        limits: Budgets итогового отчёта; None выбирает defaults.

    Raises:
        ValidationError: Невалидные limits/scope либо превышение intake budget.

    Здесь нет исполнения schema/rules/DB и не выводится соответствие projections.
    Каждый layer result обязан быть привязан к report.lineage.input_fingerprint.
    Required layers задаются явно; отсутствующая проверка становится blocker.
    """

    def __init__(
        self,
        *,
        required_layers: tuple[ValidationLayer, ...],
        limits: ProvenanceLimits | None = None,
    ) -> None:
        self._limits = BoundedInput(ProvenanceLimits()).checked(
            limits if limits is not None else ProvenanceLimits(), ProvenanceLimits
        )
        if type(required_layers) is not tuple or any(
            type(layer) is not ValidationLayer for layer in required_layers
        ):
            raise failure("VALIDATION_REPORT_SCOPE_INVALID")
        self._required = tuple(
            layer
            for layer in ValidationLayer
            if layer in (*required_layers, ValidationLayer.PROVENANCE)
        )

    def combine(
        self,
        provenance: DetailedValidationReport,
        *,
        layers: tuple[ValidationLayerResult, ...] = (),
    ) -> DetailedValidationReport:
        """Вернуть новый immutable report, сохраняя исходный отчёт и его evidence.

        Args:
            provenance: Исходный replay report без ранее объединённых layer_results.
            layers: До трёх уникальных результатов JSON Schema, DB и business rules,
                привязанных владельцем к lineage.input_fingerprint этого report.

        Returns:
            Отчёт с детерминированным порядком findings и агрегатами records.
            Отсутствующий required layer блокирует ACCEPTED; complete означает
            завершение требуемых проверок, а не отсутствие нарушений.

        Raises:
            ValidationError: Неверный scope (VALIDATION_REPORT_SCOPE_INVALID),
                иной input (VALIDATION_REPORT_INPUT_MISMATCH), malformed DTO или budget.

        I/O и запуск validators отсутствуют. Соответствие projections и подлинность
        внешних результатов обеспечивает caller; одинаковый hash этого не доказывает.
        """
        boundary = BoundedInput(self._limits)
        report = boundary.checked(provenance, DetailedValidationReport)
        if type(layers) is not tuple or len(layers) > 3:
            raise failure("VALIDATION_REPORT_SCOPE_INVALID")
        results = tuple(
            boundary.checked(item, ValidationLayerResult) for item in layers
        )
        results = tuple(
            item.model_copy(
                update={
                    "findings": tuple(
                        sorted(
                            set(item.findings),
                            key=lambda finding: finding.ordering_key(),
                        )
                    ),
                    "evidence_fingerprints": tuple(
                        sorted(set(item.evidence_fingerprints))
                    ),
                }
            )
            for item in results
        )
        if report.layer_results or len({item.layer for item in results}) != len(
            results
        ):
            raise failure("VALIDATION_REPORT_SCOPE_INVALID")
        if any(
            item.input_fingerprint != report.lineage.input_fingerprint
            for item in results
        ):
            raise failure("VALIDATION_REPORT_INPUT_MISMATCH")
        completed = tuple(
            layer
            for layer in ValidationLayer
            if layer in report.completed_layers
            or any(item.layer == layer and item.complete for item in results)
        )
        required = tuple(
            layer
            for layer in ValidationLayer
            if layer
            in (
                *self._required,
                *report.required_layers,
                *(item.layer for item in results),
            )
        )
        findings = [
            *report.findings,
            *(finding for item in results for finding in item.findings),
        ]
        for layer in required:
            if layer not in completed and not any(
                item.layer == layer and item.outcome == "unverified"
                for item in findings
            ):
                findings.append(
                    ValidationFinding(
                        layer=layer,
                        code="VALIDATION_LAYER_UNVERIFIED",
                        severity=IssueSeverity.WARNING,
                        outcome="unverified",
                    )
                )
        if len(findings) > self._limits.max_issues:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return build_report(
            run_id=report.run_id,
            generated_at=report.generated_at,
            lineage=report.lineage,
            total_records=report.total_records,
            evidence=report.evidence,
            findings=tuple(findings),
            completed_layers=completed,
            required_layers=required,
            layer_results=tuple(
                sorted(
                    results, key=lambda item: tuple(ValidationLayer).index(item.layer)
                )
            ),
        )
