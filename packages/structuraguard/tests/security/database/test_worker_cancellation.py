"""Отмена не передаёт обработанные worker/cleanup errors в event loop."""

from __future__ import annotations

import asyncio
import threading

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from structuraguard.database import InspectionLimits
from structuraguard.database._inspection import (
    InspectionControl,
    failure,
    run_inspection,
)
from structuraguard.database.postgresql import _cleanup
from structuraguard.exceptions import DatabaseInspectionError


async def drain_callbacks() -> None:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[None] = loop.create_future()
    loop.call_soon(ready.set_result, None)
    await ready


@pytest.mark.parametrize("cancel", [True, False])
@pytest.mark.parametrize("worker_error", [True, False])
def test_cancelled_worker_never_reports_handled_error_to_loop(
    cancel: bool, worker_error: bool
) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        reported: list[dict[str, object]] = []
        loop.set_exception_handler(lambda loop, context: reported.append(context))
        started = asyncio.Event()
        stopped = threading.Event()

        def worker(control: InspectionControl) -> int:
            loop.call_soon_threadsafe(started.set)
            try:
                assert control.cancelled.wait(3)
                if worker_error:
                    raise failure("PROCESSING_TIMEOUT")
                return 42
            finally:
                stopped.set()

        task = asyncio.create_task(
            run_inspection(
                worker, InspectionLimits(timeout_seconds=1, cleanup_seconds=1)
            )
        )
        async with asyncio.timeout(5):
            await started.wait()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(DatabaseInspectionError) as caught:
                    await task
                assert caught.value.error_code == "PROCESSING_TIMEOUT"
        await drain_callbacks()
        assert stopped.is_set()
        assert not reported

    asyncio.run(check())


def test_cancelled_postgresql_cleanup_does_not_report_driver_error_to_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def check() -> None:
        loop = asyncio.get_running_loop()
        reported: list[dict[str, object]] = []
        loop.set_exception_handler(lambda loop, context: reported.append(context))
        started, release = asyncio.Event(), asyncio.Event()

        async def dispose(engine: AsyncEngine, close: bool = True) -> None:
            started.set()
            await release.wait()
            raise SQLAlchemyError("password=cleanup-driver-canary")

        monkeypatch.setattr(AsyncEngine, "dispose", dispose)
        # Engine создаётся без подключения; проверяется async lifecycle, не SQL dialect.
        engine = create_async_engine(
            "postgresql+asyncpg://localhost/test", poolclass=NullPool
        )
        task = asyncio.create_task(_cleanup(None, engine, None, 1))
        async with asyncio.timeout(5):
            await started.wait()
            task.cancel()
            loop.call_soon(release.set)
            assert await task == (False, True)
        await drain_callbacks()
        assert not reported

    asyncio.run(check())
