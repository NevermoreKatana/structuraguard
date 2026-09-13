"""Ports staging и append-only audit persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import AuditEvent
from structuraguard.contracts.staging import (
    StagedBatch,
    StagedRecord,
    StagingRun,
    StagingRunSpec,
    StagingRunStatus,
)


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
class RunStagingStore(StagingStore, Protocol):
    """Lifecycle поверх stage; все операции scoped по immutable StagingContext.

    Успех stage означает атомарное сохранение metadata и заранее заданных refs,
    не разрешение load. Отмена распространяется после освобождения ресурсов.
    PostgreSQL cleanup выполняет отдельно настроенный maintenance principal.
    """

    async def begin(self, spec: StagingRunSpec) -> StagingRun:
        """Создать run без DDL; повтор полного spec возвращает прежнюю metadata.

        Args:
            spec: Полный context, fingerprints, ожидаемые counts и удерживаемые refs.

        Returns:
            Созданный OPEN run либо прежний run с тем же spec до purge.

        Raises:
            StagingError: Неверные scope, retention, expiry, budget или persistence;
                другой spec того же run даёт STAGING_RUN_EXISTS.
            asyncio.CancelledError: Отмена после освобождения ресурсов backend.

        Сохраняются только metadata и refs; внешние артефакты не открываются.
        PostgreSQL выполняет отдельную transaction; схема должна существовать.
        """

    async def get_run(self, context: StagingContext) -> StagingRun:
        """Прочитать metadata run, включая сохранённый после cleanup tombstone.

        Args:
            context: Весь исходный StagingContext, включая scope и fingerprints.

        Returns:
            Неизменяемый StagingRun с текущей revision и lifecycle status.

        Raises:
            StagingError: Run не найден, context/policy не совпали либо backend недоступен.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет run и не открывает artifacts. Полный JSON metadata чувствителен.
        """

    async def seal(
        self, context: StagingContext, *, expected_revision: int
    ) -> StagingRun:
        """Закрыть полный staged snapshot для дальнейшего append через CAS.

        Args:
            context: Полный context существующего OPEN run.
            expected_revision: Точная revision, полученная через get_run.

        Returns:
            SEALED run с новой revision и sealed_fingerprint.

        Raises:
            StagingError: Не совпали revision/counts, истёк срок, неверны state/backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Меняет только metadata. Caller завершает внешний iterator до seal;
        проверки terminal manifest в stage не заменяют M11/M12 и physical replay.
        """

    async def read_records(
        self, context: StagingContext, *, offset: int, limit: int
    ) -> tuple[StagedRecord, ...]:
        """Прочитать ограниченную страницу metadata закрытого для append run.

        Args:
            context: Полный context run.
            offset: Неотрицательное смещение в record index.
            limit: Размер страницы от 1 до limits.max_page_records.

        Returns:
            Tuple StagedRecord в исходном порядке; за концом index — пустой tuple.

        Raises:
            StagingError: OPEN/purged run, истёкшие refs/retention, неверные context,
                пределы страницы либо ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет metadata и не открывает refs. PostgreSQL проверяет весь bounded
        run при каждом чтении; размер страницы не ограничивает стоимость проверки.
        """

    async def read_batches(
        self, context: StagingContext, *, offset: int, limit: int
    ) -> tuple[StagedBatch, ...]:
        """Прочитать страницу batch summaries, в том числе для OPEN run.

        Args:
            context: Полный context run.
            offset: Неотрицательное смещение в batch index.
            limit: Размер страницы от 1 до limits.max_page_records.

        Returns:
            Tuple StagedBatch в порядке batches; за концом index — пустой tuple.

        Raises:
            StagingError: Purge, истёкшая retention, неверный context, размер или backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет metadata и не читает payload. Стоимость PostgreSQL проверки
        пропорциональна всему bounded run, даже для короткой страницы.
        """

    async def transition(
        self,
        context: StagingContext,
        *,
        expected_revision: int,
        status: StagingRunStatus,
    ) -> StagingRun:
        """Записать разрешённый lifecycle transition с проверкой revision.

        Args:
            context: Полный context run.
            expected_revision: Текущая revision для CAS.
            status: Разрешённое следующее состояние StagingRunStatus.

        Returns:
            Run с новым status, revision и рассчитанным cleanup_after.

        Raises:
            StagingError: Конфликт revision, запрещённый переход или ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Меняет metadata; caller отвечает за истинность объявленного DB outcome.
        UNKNOWN не истекает автоматически. Terminal run не возвращается в OPEN.
        """

    async def cleanup(self, context: StagingContext) -> StagingRun:
        """Применить retention к одному run, сохранив tombstone и его идентичность.

        Args:
            context: Полный context run; PostgreSQL требует maintenance target.

        Returns:
            Текущий run либо очищенный tombstone с purged=True без refs и index.

        Raises:
            StagingError: Неверный context, запрещённая роль или ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Истёкший OPEN/SEALED получает EXPIRED; EXECUTING/UNKNOWN сохраняются.
        После retention удаляются batch/record metadata и refs. Внешние artifacts
        и loader ledger не удаляются; run_id остаётся занят.
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
