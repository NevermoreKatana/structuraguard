"""Port профилирования завершённого normalized stream."""

from collections.abc import AsyncIterable
from typing import Protocol, runtime_checkable

from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.profiling import (
    NormalizedDataProfile,
    NormalizedProfileContext,
)


@runtime_checkable
class NormalizedDataProfiler(Protocol):
    """Потребляет поток одного semantic execution с bounded state."""

    async def profile(
        self,
        batches: NormalizedBatch | AsyncIterable[NormalizedBatch],
        *,
        context: NormalizedProfileContext | None = None,
    ) -> NormalizedDataProfile:
        """Потребить один полный dataset после семантического разбора.

        Args:
            batches: Полный normalized stream либо одиночный terminal batch.
            context: Минимальный класс и контекст полей; caller передаёт
                classification источника и lineage для supplied labels.

        Returns:
            Профиль после terminal, EOF и закрытия полученного iterator.
            Исходные данные не изменяются; source/provider ресурсами владеет caller.

        Raises:
            NormalizedProfilingError: Нарушение stream, лимитов, классификации
                или cleanup; незавершённый профиль не возвращается.
            asyncio.CancelledError: Отмена с сохранением cleanup semantics.

        Полный DTO чувствителен; для logs используется только safe_summary().
        Метод не выдаёт разрешений на mapping, загрузку или внешний egress.
        """
        ...
