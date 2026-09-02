"""Синхронная оболочка над async-first facade."""

import asyncio
from typing import Never

from .config import SDKConfig
from .exceptions import StructuraGuardError
from .sdk import AsyncStructuraGuard


class StructuraGuard:
    """Отдельная синхронная оболочка над ``AsyncStructuraGuard``.

    Args:
        config: Явная неизменяемая конфигурация. Если значение не передано,
            создаётся отдельный ``SDKConfig`` для этого экземпляра.

    Создание оболочки не читает переменные окружения, не создаёт event loop или
    thread и не выполняет I/O. Вызов операции вне активного event loop использует
    временный loop через ``asyncio.run``. Внутри активного loop следует
    использовать ``AsyncStructuraGuard``.
    """

    __slots__ = ("_async_sdk",)

    def __init__(self, *, config: SDKConfig | None = None) -> None:
        self._async_sdk = AsyncStructuraGuard(config=config)

    @property
    def config(self) -> SDKConfig:
        """Вернуть instance-local конфигурацию обёрнутого async facade."""

        return self._async_sdk.config

    def inspect_source(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``inspect_source``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.inspect_source(*args, **kwargs))

    def inspect_database(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``inspect_database``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.inspect_database(*args, **kwargs))

    def create_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``create_plan``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.create_plan(*args, **kwargs))

    def validate_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``validate_plan``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.validate_plan(*args, **kwargs))

    def execute(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``execute``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.execute(*args, **kwargs))

    def analyze(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``analyze``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.analyze(*args, **kwargs))

    def ingest(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``ingest``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.ingest(*args, **kwargs))

    def propose_schema(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``propose_schema``.

        Args:
            *args: Позиционные аргументы для передачи async facade.
            **kwargs: Именованные аргументы для передачи async facade.

        Raises:
            StructuraGuardError: Если вызов сделан внутри активного event loop;
                ``error_code`` равен ``SYNC_API_IN_ASYNC_CONTEXT``.
            OperationNotImplementedError: Всегда вне активного event loop.

        Метод не возвращает результат и не выполняет предметный I/O.
        """

        self._ensure_sync_context()
        asyncio.run(self._async_sdk.propose_schema(*args, **kwargs))

    @staticmethod
    def _ensure_sync_context() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise StructuraGuardError(
            error_code="SYNC_API_IN_ASYNC_CONTEXT",
            message=(
                "Sync facade нельзя вызывать внутри работающего event loop; "
                "используйте AsyncStructuraGuard."
            ),
            retryable=False,
        )
