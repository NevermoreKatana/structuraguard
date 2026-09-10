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
    StructureProfile,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import ExtractedBatch


@runtime_checkable
class StructuralProfiler(Protocol):
    """Строит bounded профиль завершённого extraction без DB/LLM и execution."""

    async def profile(
        self,
        batches: ExtractedBatch | AsyncIterable[ExtractedBatch],
    ) -> StructureProfile:
        """Прочитать terminal batch либо весь поток с terminal manifest.

        Args:
            batches: Один terminal batch либо полный async stream одного extraction.

        Returns:
            Профиль с coverage, evidence и всеми удержанными кандидатами.
            Пустой source даёт пустой профиль, а не выдуманные references.

        Security:
            Реализация проверяет stream, применяет конечные budgets и закрывает
            полученный iterator; full source не удерживается в памяти.
        """


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
            Созданный plan, ranked кандидаты, NEEDS_SEMANTIC_ANALYSIS
            при нехватке evidence либо отклонение неподдержанного mode.

        Security:
            Результат остаётся недоверенным и сам не разрешает execution.
            Deterministic реализация M5 требует physical replay через расширенный
            analyze(request, batches=replay); вызов без replay возвращает
            NEEDS_SEMANTIC_ANALYSIS с issue STRUCTURE_REPLAY_REQUIRED.
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

        Реализация M5 без physical replay возвращает REJECTED с
        PARSE_PLAN_REPLAY_REQUIRED. Для acceptance нужны её расширенные
        validate(request, batches=iterable) либо async validate_source.
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
            ``NormalizedBatch`` с physical provenance. Non-terminal batches
            предварительны до EOF, проверки refs и успешного cleanup.

        Raises:
            ParseExecutionError: Typed issue при нарушении plan/source/limits.
            asyncio.CancelledError: Отмена после cleanup owned iterator.

        Security:
            Сигнатура принимает только ``ValidatedParsePlan`` и не содержит
            execution callbacks. Строковые значения остаются данными: port не
            разрешает исполнять их содержимое. Wrapper и source перепроверяются;
            caller обеспечивает staging/rollback и aclose при раннем выходе.
        """
