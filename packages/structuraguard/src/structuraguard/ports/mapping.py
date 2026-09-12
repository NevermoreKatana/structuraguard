"""Ports ранжирования semantic fields и проверки MappingPlan без DB/LLM handles."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingResult,
    MappingScope,
)
from structuraguard.contracts.mapping import MappingPlan, MappingPlanValidationResult
from structuraguard.contracts.mapping_validation import MappingPlanInputReport
from structuraguard.contracts.normalized import NormalizedDatasetManifest
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog


@runtime_checkable
class MappingPlanValidator(Protocol):
    """Независимо проверить декларативный план по снимкам без полномочий на запись.

    Реализация получает policy через явную конфигурацию, а не из плана.
    JSON intake не входит в этот port и может быть отдельным методом реализации.
    """

    async def validate(
        self,
        plan: MappingPlan,
        manifest: NormalizedDatasetManifest,
        catalog: DatabaseCatalog,
        *,
        profile: NormalizedDataProfile | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        """Собрать все применимые независимые issues в детерминированном порядке.

        Args:
            plan: Декларация; форма и fingerprints проверяются независимо.
            manifest: Связанный снимок normalized данных.
            catalog: Снимок metadata БД с target/policy binding.
            profile: Необязательный профиль того же manifest для проверки типов.

        Returns:
            Result с evidence и wrapper только при ACCEPTED либо intake report
            при непригодном плане. Версии снимков и лимиты определяет реализация.

        Raises:
            structuraguard.exceptions.ValidationError: Непригодные снимки или
                превышение лимита; error_code уточняет причину.
            asyncio.CancelledError: Отмена без частичного результата.

        Side effects:
            Реализация не изменяет снимки, не читает source/DB и не вызывает LLM.

        Security:
            Полный result чувствителен и не является разрешением на загрузку.
            Caller отвечает за происхождение снимков и проверку живой БД.
        """
        ...


@runtime_checkable
class CandidateMapper(Protocol):
    """Вернуть bounded candidates, сохранив неоднозначность и lineage."""

    async def rank(
        self,
        profile: NormalizedDataProfile,
        catalog: DatabaseCatalog,
        *,
        scope: MappingScope,
        semantic_catalog: DatabaseSemanticCatalog | dict[str, object] | None = None,
    ) -> DeterministicMappingResult:
        """Ранжировать готовые snapshots без I/O и разрешения на загрузку.

        Args:
            profile: Завершённый профиль normalized данных с semantic field refs.
            catalog: Каталог БД с target/policy binding и FK graph.
            scope: Trusted allow/deny refs; пустой allow запрещает все targets.
            semantic_catalog: Необязательные локальные semantic hints как DTO/dict.

        Returns:
            Bounded candidates, SDK scores, explanations, ambiguity и lineage.
            Поддержанные schema versions и ceilings определяет реализация.

        Raises:
            MappingError: Полный отказ по входам, bindings или resource budget.
            DatabaseInspectionError: Несовпадение schema fingerprint.
            asyncio.CancelledError: Отмена; partial result не возвращается.

        Side effects:
            Реализация не читает source/DB, не вызывает LLM и не изменяет snapshots.

        Security:
            Полный result чувствителен и не является approval. Caller отвечает
            за происхождение snapshots, scope и проверку живой DB перед load.
        """
        ...
