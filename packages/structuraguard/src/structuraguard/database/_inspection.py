"""Bounded worker lifecycle для blocking SQLite driver."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field

from structuraguard.exceptions import DatabaseInspectionError

from .target import InspectionLimits


def failure(code: str) -> DatabaseInspectionError:
    return DatabaseInspectionError(
        error_code=code,
        message="Инспекция БД не завершена; каталог не опубликован.",
    )


@contextmanager
def inspection_slot(lock: threading.Lock, *, unavailable: bool) -> Iterator[None]:
    if unavailable or not lock.acquire(blocking=False):
        raise failure("DATABASE_INSPECTION_BUSY")
    try:
        yield
    finally:
        lock.release()


@dataclass(slots=True)
class InspectionControl:
    limits: InspectionLimits
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)
    _connection: sqlite3.Connection | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    items: int = 0
    metadata_bytes: int = 0
    statement_deadline: float | None = None

    def attach(self, connection: sqlite3.Connection) -> None:
        with self._lock:
            self._connection = connection
        self.check()

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def cancel(self) -> None:
        self.cancelled.set()
        with self._lock:
            if self._connection is not None:
                # SQLAlchemy мог уже закрыть raw connection при выходе.
                with suppress(sqlite3.ProgrammingError):
                    self._connection.interrupt()

    def expired(self) -> bool:
        now = time.monotonic()
        return (
            self.cancelled.is_set()
            or now >= self.deadline
            or (self.statement_deadline is not None and now >= self.statement_deadline)
        )

    def check(self) -> None:
        if self.expired():
            raise failure("PROCESSING_TIMEOUT")

    def account(self, values: tuple[object, ...]) -> None:
        self.check()
        self.items += 1
        self.metadata_bytes += sum(
            len(value.encode("utf-8")) if isinstance(value, str) else 8
            for value in values
        )
        if (
            self.items > self.limits.max_items
            or self.metadata_bytes > self.limits.max_metadata_bytes
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")


async def run_inspection[Result](
    operation: Callable[[InspectionControl], Result], limits: InspectionLimits
) -> Result:
    control = InspectionControl(limits, time.monotonic() + limits.timeout_seconds)
    task = asyncio.create_task(asyncio.to_thread(operation, control))
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            return await asyncio.shield(task)
    except (asyncio.CancelledError, TimeoutError):
        control.cancel()
        # Дождаться завершения worker: to_thread cancellation сама не закрывает DB.
        cleanup_deadline = time.monotonic() + limits.cleanup_seconds
        while not task.done():
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                task.add_done_callback(_consume_result)
                raise failure("DATABASE_INSPECTION_CLEANUP_FAILED") from None
            try:
                await asyncio.wait_for(asyncio.shield(task), remaining)
            except asyncio.CancelledError:
                control.cancel()
            except DatabaseInspectionError:
                break
            except TimeoutError:
                task.add_done_callback(_consume_result)
                raise failure("DATABASE_INSPECTION_CLEANUP_FAILED") from None
        if task.done():
            _consume_result(task)
        caller = asyncio.current_task()
        if caller is not None and caller.cancelling():
            raise asyncio.CancelledError from None
        raise failure("PROCESSING_TIMEOUT") from None


def _consume_result(task: asyncio.Task[object]) -> None:
    if not task.cancelled():
        task.exception()
