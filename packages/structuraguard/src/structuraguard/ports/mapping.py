"""Port ранжирования semantic fields без DB/LLM handles."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingResult,
    MappingScope,
)
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog


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
