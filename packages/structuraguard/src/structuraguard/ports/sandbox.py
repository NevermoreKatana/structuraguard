"""Host OS isolation port. Утверждение plugin о capabilities не принимается."""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Protocol, runtime_checkable

from structuraguard.contracts.sandbox import (
    SandboxCapabilities,
    SandboxExit,
    SandboxRequest,
)
from structuraguard.contracts.source import ExtractedBatch, SourceArtifact
from structuraguard.ports.source import ParseContext, SourceReader


@runtime_checkable
class ParserRunner(Protocol):
    """Поток verified extraction через async context manager с явным cleanup.

    Source/context недоверенны; batches промежуточные до подтверждённого EOF.
    Policy/limits дают typed errors; отмена распространяется после cleanup.
    Наличие protocol не обеспечивает OS isolation.
    """

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AbstractAsyncContextManager[AsyncIterator[ExtractedBatch]]:
        """Выдать проверяемый stream; early exit обязан закрыть runner resources."""


@runtime_checkable
class SandboxProcess(Protocol):
    """Управлять одним worker без передачи raw stdout/stderr в diagnostics.

    Backend ограничивает IPC до накопления. Aclose обязателен при ошибке,
    timeout и early exit; cleanup_complete подтверждает kill/reap/temp cleanup.
    """

    async def probe(self, *, max_bytes: int) -> bytes:
        """Получить один bounded JSON ProbeResult; oversized frame не накапливать."""

    async def read_frame(self, *, max_bytes: int) -> bytes | None:
        """Получить bounded JSON ExtractedBatch либо EOF; stderr не публиковать."""

    async def wait(self) -> SandboxExit:
        """Дождаться worker и вернуть status без stdout/stderr/command strings."""

    async def aclose(self) -> SandboxExit:
        """Terminate process tree, reap и убрать temp, в том числе после cancel."""


@runtime_checkable
class SandboxBackend(Protocol):
    """Trusted host adapter реальной OS isolation, отсутствующий в core SDK.

    Declared capabilities должны быть обеспечены deployment и проверены host.
    Ошибки/отмена не должны раскрывать secrets; start получает только spec,
    bounded source lease и limits. Shell interpolation запрещена.
    """

    @property
    def capabilities(self) -> SandboxCapabilities:
        """Declaration доверенного adapter owner, закреплённая до запуска."""

    async def start(
        self, request: SandboxRequest, reader: SourceReader
    ) -> SandboxProcess:
        """Запустить изолированный worker с pinned artifact и bounded source lease.

        Resolution/import/constructor происходят только в sandbox. Request не
        содержит host paths, DB/LLM/audit handles. Backend передаёт argv напрямую,
        без shell interpolation. Ограничивает stdout/stderr/frames до накопления.
        Отмена start требует terminate/reap даже если process ещё не возвращён.
        Host backend обязан соблюдать переданные caps; core не создаёт container.
        """
