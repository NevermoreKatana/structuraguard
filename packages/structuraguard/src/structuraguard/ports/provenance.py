"""Physical replay boundary без source handles и доступа к storage."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.common import UtcDateTime
from structuraguard.contracts.normalization import NormalizationResult
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import ParseExecutionContext, ValidatedParsePlan
from structuraguard.contracts.provenance import DetailedValidationReport
from structuraguard.contracts.source import ExtractedBatch


@runtime_checkable
class ProvenanceValidator(Protocol):
    """Проверить связанные snapshots; typed refusal/cancellation не являются pass."""

    async def validate(
        self,
        batches: tuple[NormalizedBatch, ...],
        *,
        source_batches: tuple[ExtractedBatch, ...],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
        generated_at: UtcDateTime,
        normalizations: tuple[NormalizationResult, ...] = (),
    ) -> DetailedValidationReport:
        """Проверить provenance/normalization завершённых snapshots без I/O.

        Args:
            batches: Полный normalized snapshot после EOF.
            source_batches: Physical snapshot того же source.
            plan: Доверенный проверенный ParsePlan.
            context: Контекст исходного M5 исполнения с source fingerprint.
            generated_at: Явное время отчёта в UTC.
            normalizations: Sidecars для настроенных владельцем bindings.

        Returns:
            Immutable DetailedValidationReport с references и findings.
            Полный report чувствителен; safe_summary предназначен для logs.

        Raises:
            ValidationError: Невалидный DTO/config или исчерпание budget.
                Refusal/cancellation не превращаются в успешный report.

        Значения и locations не изменяются; locations не открываются.
        """
        ...
