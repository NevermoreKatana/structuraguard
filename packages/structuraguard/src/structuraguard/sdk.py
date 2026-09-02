"""Асинхронный публичный facade StructuraGuard."""

from typing import Never

from .config import SDKConfig
from .exceptions import OperationNotImplementedError


class AsyncStructuraGuard:
    """Основная асинхронная точка входа SDK M1.

    Args:
        config: Явная неизменяемая конфигурация. Если значение не передано,
            создаётся отдельный ``SDKConfig`` для этого экземпляра.

    Создание facade не читает переменные окружения, не создаёт event loop или
    thread и не выполняет файловый либо сетевой I/O. В M1 операции pipeline не
    реализованы и завершаются ``OperationNotImplementedError``.
    """

    __slots__ = ("_config",)

    def __init__(self, *, config: SDKConfig | None = None) -> None:
        self._config = config if config is not None else SDKConfig()

    @property
    def config(self) -> SDKConfig:
        """Вернуть instance-local ``SDKConfig``, переданный при создании."""

        return self._config

    async def inspect_source(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``inspect_source``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("inspect_source")

    async def inspect_database(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``inspect_database``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("inspect_database")

    async def create_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``create_plan``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("create_plan")

    async def validate_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``validate_plan``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("validate_plan")

    async def execute(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``execute``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("execute")

    async def analyze(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``analyze``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("analyze")

    async def ingest(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``ingest``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("ingest")

    async def propose_schema(self, *args: object, **kwargs: object) -> Never:
        """Отклонить недоступную в M1 операцию ``propose_schema``.

        Args:
            *args: Позиционные аргументы, которые в M1 не интерпретируются.
            **kwargs: Именованные аргументы, которые в M1 не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("propose_schema")

    @staticmethod
    def _operation_unavailable(operation: str) -> Never:
        raise OperationNotImplementedError(operation)
