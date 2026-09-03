"""Port read-only inspection и исполнения проверенного MappingPlan."""

from __future__ import annotations

from collections.abc import AsyncIterable
from typing import Protocol, runtime_checkable

from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    LoadContext,
)
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import LoadReport


@runtime_checkable
class DatabaseAdapter(Protocol):
    """Анализирует каталог read-only и пишет только по checked plan."""

    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        """Получить read-only snapshot структуры целевой БД.

        Args:
            request: Непрозрачный target ID, policy fingerprint и schema filter.

        Returns:
            Fingerprint-bound каталог без DSN и credentials.

        Security:
            Операция не изменяет схему или данные целевой БД.
        """

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: ValidatedMappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        """Выполнить загрузку по независимо проверенному mapping plan.

        Args:
            batches: Нормализованные batches с проверяемой lineage.
            plan: Проверенный ``MappingPlan`` без SQL.
            context: Target, policy и safety evidence текущей загрузки.

        Returns:
            Отчёт о dry-run, commit, rollback либо незавершённой загрузке с
            доступным transaction evidence.

        Side effects:
            В write-режиме адаптер изменяет данные только после повторной
            проверки target и fingerprints; SQL формирует сам адаптер.
        """
