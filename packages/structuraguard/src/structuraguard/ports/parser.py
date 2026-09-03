"""Port технического извлечения физической структуры источника."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.ports.source import ParseContext, ProbeContext


@runtime_checkable
class Parser(Protocol):
    """Извлекает raw values и physical provenance без бизнес-семантики."""

    @property
    def adapter_id(self) -> str:
        """Вернуть неизменяемый идентификатор parser adapter."""

    @property
    def version(self) -> str:
        """Вернуть версию parser adapter."""

    @property
    def priority(self) -> int:
        """Вернуть стабильный приоритет выбора adapter."""

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Оценить применимость parser к ограниченному snapshot.

        Args:
            source: Метаданные неизменяемого source snapshot.
            context: Fingerprint-bound reader и предел чтения для probe.

        Returns:
            Технический результат определения формата без бизнес-семантики.

        Security:
            Адаптер читает источник только через bounded ``context.reader``.
        """

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Потоково извлечь raw values и их physical provenance.

        Args:
            source: Метаданные неизменяемого source snapshot.
            context: Fingerprint-bound reader и resource limits parsing.

        Yields:
            ``ExtractedBatch`` без окончательной бизнес-семантики.

        Security:
            Адаптер соблюдает limits контекста и не получает LLM или DB ports.
        """
