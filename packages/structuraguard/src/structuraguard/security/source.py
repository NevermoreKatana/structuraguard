"""Bounded source transport: проверка размера до накопления snapshot."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from structuraguard.contracts.security import Resource
from structuraguard.contracts.source import SourceArtifact
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.resources import SourceStream
from structuraguard.ports.source import SourceReader

if TYPE_CHECKING:
    from .session import SecuritySession


async def _read_source(
    session: SecuritySession, operation: Callable[[], Awaitable[bytes]]
) -> bytes:
    """Source adapter не получает authority для raw diagnostics, даже typed errors."""
    try:
        return await operation()
    except asyncio.CancelledError:
        session.record_cancelled()
        raise asyncio.CancelledError from None
    except BaseException as error:
        # Внешний transport может включить source/credentials в любой Exception.
        if not isinstance(error, Exception):
            raise
        if session.events:
            event = session.events[0]
            session.deny(
                event.code, event.resource, limit=event.limit, observed=event.observed
            )
        session.deny("SECURITY_OPERATION_FAILED")


class BoundedSnapshot:
    """Immutable snapshot, полученный через SecuritySession.snapshot.

    data — уже ограниченные bytes; прямой конструктор не проверяет policy.
    Fingerprint не является аутентификацией источника. Repr не раскрывает payload;
    read возвращает raw bytes без I/O, которые host обязан защищать."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._fingerprint = "sha256:" + hashlib.sha256(data).hexdigest()

    @property
    def source_fingerprint(self) -> str:
        """Вернуть SHA-256 содержимого для binding, не для безопасного логирования."""
        return self._fingerprint

    @property
    def size_bytes(self) -> int:
        """Вернуть длину snapshot в bytes без чтения внешнего источника."""
        return len(self._data)

    async def read(self, *, offset: int, size: int) -> bytes:
        """Вернуть не более size bytes с offset; за EOF возвращает пустые bytes.

        Offset/size — неотрицательные strict int; иначе SecurityPolicyError/
        SECURITY_INPUT_REJECTED. Read не выполняет I/O и не расходует run counters:
        policy/deadline при parsing проверяет guarded reader."""
        if type(offset) is not int or type(size) is not int or offset < 0 or size < 0:
            raise SecurityPolicyError(
                error_code="SECURITY_INPUT_REJECTED", message="Некорректный read range."
            )
        return self._data[offset : offset + size]


async def snapshot(
    session: SecuritySession,
    reader: SourceStream,
    *,
    kind: str,
    expected_size: int | None,
) -> BoundedSnapshot:
    if type(kind) is not str or kind not in {"file", "stream"}:
        session.deny("SECURITY_INPUT_REJECTED")
    resource = Resource.FILE_BYTES if kind == "file" else Resource.STREAM_BYTES
    if expected_size is not None:
        session.check(resource, expected_size)
    data = bytearray()
    while True:
        remaining = session.policy.limits.maximum(resource) - len(data)
        requested = min(session.policy.limits.read_chunk_bytes, remaining + 1)
        chunk = await session.call(
            partial(_read_source, session, partial(reader.read, requested))
        )
        if type(chunk) is not bytes or len(chunk) > requested:
            session.deny("SECURITY_INPUT_REJECTED")
        if not chunk:
            if expected_size is not None and len(data) != expected_size:
                session.deny("SECURITY_INPUT_REJECTED")
            return BoundedSnapshot(bytes(data))
        session.check(resource, len(data) + len(chunk))
        if expected_size is not None and len(data) + len(chunk) > expected_size:
            session.deny("SECURITY_INPUT_REJECTED")
        data.extend(chunk)


class GuardedReader:
    """Ограничить внешние read и связать их с source fingerprint/размером.

    reader/source/session принадлежат trusted composition. Создавайте lease через
    SecuritySession.guarded_reader. Raw transport exceptions санитизируются;
    scope/roots/endpoint и закрытие transport остаются ответственностью host."""

    def __init__(
        self, reader: SourceReader, source: SourceArtifact, session: SecuritySession
    ) -> None:
        self._reader, self._source, self._session = reader, source, session

    @property
    def source_fingerprint(self) -> str:
        """Вернуть ожидаемый fingerprint source; actual reader сверяется при read."""
        return self._source.source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        """Вернуть bounded raw bytes после range/deadline/fingerprint проверок.

        Offset/size ограничены source и read cap. Drift, over-return, чужая ошибка
        и timeout дают SecurityPolicyError и terminal event без raw diagnostics.
        Await распространяет отмену; transport открывает и закрывает host."""
        self._session.remaining_seconds(Resource.PARSER_TIME_MS)
        return await _read_source(
            self._session, partial(self._read, offset=offset, size=size)
        )

    async def _read(self, *, offset: int, size: int) -> bytes:
        run = self._session
        if (
            type(offset) is not int
            or type(size) is not int
            or offset < 0
            or size <= 0
            or offset > self._source.size_bytes
            or self._reader.source_fingerprint != self.source_fingerprint
        ):
            run.deny("SECURITY_INPUT_REJECTED")
        requested = min(
            size, run.policy.limits.read_chunk_bytes, self._source.size_bytes - offset
        )
        if not requested:
            return b""
        result = await self._reader.read(offset=offset, size=requested)
        if type(result) is not bytes or len(result) > requested:
            run.deny("SECURITY_INPUT_REJECTED")
        return result
