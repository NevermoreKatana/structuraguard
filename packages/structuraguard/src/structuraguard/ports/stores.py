"""Ports staging и append-only audit persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import AuditEvent


@runtime_checkable
class StagingStore(Protocol):
    """Сохраняет normalized batch в bounded staging scope."""

    async def stage(self, batch: NormalizedBatch, context: StagingContext) -> None:
        """Зафиксировать batch в staging до разрешения target write.

        Args:
            batch: Нормализованный batch с проверяемой lineage.
            context: Bounded staging scope текущего run и target.

        Returns:
            ``None`` после завершения записи в staging.

        Side effects:
            Операция изменяет только заданный staging scope и не разрешает
            запись в целевую БД.
        """


@runtime_checkable
class AuditStore(Protocol):
    """Добавляет immutable audit events без произвольного raw payload."""

    async def append(self, event: AuditEvent) -> None:
        """Надёжно добавить одно immutable событие в append-only журнал.

        Args:
            event: Типизированное событие без произвольного raw payload.

        Returns:
            ``None`` после завершения append.

        Side effects:
            Операция дописывает журнал; существующие события не изменяются.
            Неуспешная запись должна завершиться явной ошибкой.
        """
