"""Ports построения, проверки и применения декларативного ParsePlan."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from typing import Protocol, runtime_checkable

from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
    StructureAnalysisRequest,
    StructureAnalysisResult,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import ExtractedBatch


@runtime_checkable
class SemanticStructureAnalyzer(Protocol):
    """Формирует недоверенный структурированный результат анализа источника."""

    async def analyze(
        self,
        request: StructureAnalysisRequest,
    ) -> StructureAnalysisResult:
        """Проанализировать физическую структуру из manifest и profile.

        Args:
            request: Manifest, typed profile и fingerprint-bound bounded raw samples
                одного extraction run.

        Returns:
            Созданный plan, запрос проверки кандидатов или отклонение.

        Security:
            Результат остаётся недоверенным и сам не разрешает execution.
        """


@runtime_checkable
class ParsePlanValidator(Protocol):
    """Независимо проверяет ParsePlan и его source references."""

    def validate(
        self,
        request: ParsePlanValidationRequest,
    ) -> ParsePlanValidationResult:
        """Проверить ``ParsePlan`` и его lineage без I/O.

        Args:
            request: Plan, source, manifest и profile одного snapshot.

        Returns:
            Решение проверки; checked wrapper доступен только при принятии.
        """


@runtime_checkable
class ParsePlanExecutor(Protocol):
    """Применяет только проверенный ParsePlan к extracted batches."""

    def execute(
        self,
        batches: AsyncIterable[ExtractedBatch],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncIterator[NormalizedBatch]:
        """Потоково применить проверенный plan без повторного анализа.

        Args:
            batches: Физические batches, заявленные для manifest контекста.
            plan: Независимо проверенный ``ParsePlan``.
            context: Lineage и limits текущего execution run.

        Yields:
            ``NormalizedBatch`` с physical provenance.

        Security:
            Сигнатура принимает только ``ValidatedParsePlan`` и не содержит
            execution callbacks. Строковые значения остаются данными: port не
            разрешает исполнять их содержимое.
        """
