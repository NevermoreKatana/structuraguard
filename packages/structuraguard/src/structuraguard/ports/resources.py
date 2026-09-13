"""Run-local resource hooks для adapters; без SQL, raw payload и key material."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Never, Protocol
from uuid import UUID

from structuraguard.contracts.security import (
    Resource,
    ResourceErrorCode,
    SecurityLimits,
    SecurityPolicy,
)
from structuraguard.contracts.source import ExtractedBatch, SourceArtifact
from structuraguard.ports.parser import Parser
from structuraguard.ports.source import ParseContext, SourceReader


class ResourceGuard(Protocol):
    """Lifecycle evidence и deadline для trusted adapter operations."""

    async def call[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        resource: Resource = Resource.PROCESSING_TIME_MS,
    ) -> T:
        """Ограничить await; только операции без необратимого COMMIT."""

    def remaining_seconds(self, resource: Resource) -> float:
        """Проверить deadline перед началом операции."""

    def record_failure(self, code: str) -> None:
        """Записать terminal event; произвольный code не попадает в event."""

    def record_cancelled(self) -> None:
        """Записать отмену после завершения adapter cleanup."""


class DatabaseResourceGuard(ResourceGuard, Protocol):
    """Перед каждым statement резервирует общий DB budget текущего run."""

    @property
    def limits(self) -> SecurityLimits:
        """Trusted maxima для компиляции существующих batch policies."""

    def check_deadline(self) -> None:
        """Проверить общий deadline до commit, не расходуя query reservation."""

    def before_query(self) -> None:
        """Проверить deadline и зарезервировать query до driver invocation."""


class LLMResourceGuard(ResourceGuard, Protocol):
    """Дополнительный run maximum; локальным attempt accounting владеет router."""

    def reserve_llm(self, tokens: int) -> None:
        """Атомарно зарезервировать call и полный token allowance до egress."""

    def llm_remaining_seconds(self) -> float:
        """Вернуть общий оставшийся deadline для следующего await."""


class SourceStream(Protocol):
    """Caller-owned transport; сам SDK не открывает paths или remote URL."""

    async def read(self, size: int) -> bytes:
        """Вернуть максимум size bytes; пустые bytes означают EOF."""


class ParserResourceGuard(Protocol):
    """Связать runner с общим run без зависимости от security facade.

    Trusted host реализует общий budget и terminal state. Check/deadline/deny
    дают SecurityPolicyError; первый отказ/ошибка/отмена закрывает run также
    для DB/LLM operations. Нельзя реализовывать record_failure как no-op.
    Evidence не содержит raw input/diagnostics; persistence подключает host.
    """

    @property
    def policy(self) -> SecurityPolicy:
        """Вернуть immutable policy текущего run без расширения authority."""
        ...

    @property
    def limits(self) -> SecurityLimits:
        """Вернуть максимумы, сужающие существующие локальные limits."""
        ...

    @property
    def run_id(self) -> UUID:
        """Вернуть UUID общего run, назначенный trusted host."""
        ...

    def check(self, resource: Resource, value: int) -> None:
        """Проверить включительный максимум до накопления; отказ закрывает run."""
        ...

    def remaining_seconds(self, resource: Resource) -> float:
        """Вернуть положительный минимум stage/run deadline или отказать."""
        ...

    def cancel(self) -> None:
        """Закрыть общий run отменой без raw текста исключения."""
        ...

    def record_failure(self, code: str) -> None:
        """Закрыть общий run безопасным event, сохраняя original runner error."""

    def deny(
        self,
        code: ResourceErrorCode = "SECURITY_INPUT_REJECTED",
        resource: Resource | None = None,
        *,
        limit: int | None = None,
        observed: int | None = None,
    ) -> Never:
        """Закрыть run и поднять SecurityPolicyError с закрытым code/resource.

        Limit/observed — безопасные числа; raw данные и произвольный текст
        не допускаются. Первый terminal event не вытесняется поздним отказом.
        """
        ...

    def guarded_reader(
        self, reader: SourceReader, source: SourceArtifact
    ) -> SourceReader:
        """Создать fingerprint-bound lease с limits текущего run."""

    def parse(
        self, parser: Parser, source: SourceArtifact, context: ParseContext
    ) -> AbstractAsyncContextManager[AsyncIterator[ExtractedBatch]]:
        """Выполнить trusted parser с policy/resource guards и cleanup."""
