"""Ограниченное in-memory staging для локальных workflows и contract tests."""

import asyncio
from collections.abc import Callable

from structuraguard.contracts.database import StagingContext

from ._core import Access, StagingOperations, State, failure


class MemoryStagingStore(StagingOperations):
    """Хранить metadata и refs внутри экземпляра без внешнего I/O.

    Args:
        target_id: Доверенный идентификатор целевого scope.
        retention: Неизменяемая policy хранения refs и run.
        limits: Конечные бюджеты одного run; None — StagingLimits по умолчанию.
        clock: Доверенные часы datetime с tzinfo=UTC; None — системное время UTC.

    Raises:
        StagingError: Неверные limits или retention при создании экземпляра.

    Durable storage и crash recovery отсутствуют. Максимум 100 run tombstones;
    лимиты одного run не являются общим memory budget приложения. Артефакты
    по references не открываются. Настоящий dry-run не требует этого store.
    """

    async def _access[T](
        self,
        context: StagingContext,
        mode: Access,
        operation: Callable[[State | None], tuple[State, T]],
    ) -> T:
        # Lazy instance storage не создаёт loop-affine resources при import.
        if not hasattr(self, "_lock"):
            self._lock = asyncio.Lock()
            self._runs: dict[str, State] = {}
        async with self._lock:
            before = self._runs.get(context.run_id)
            if before is None and len(self._runs) >= 100:
                raise failure("STAGING_LIMIT_EXCEEDED")
            state, result = operation(before)
            self._runs[context.run_id] = state
            return result
