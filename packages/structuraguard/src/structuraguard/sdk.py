"""Асинхронный публичный facade StructuraGuard."""

from typing import Never

from .config import SDKConfig
from .exceptions import OperationNotImplementedError
from .parsers import ParserRegistry


class AsyncStructuraGuard:
    """Основная асинхронная точка входа SDK.

    Args:
        config: Явная неизменяемая конфигурация. Если значение не передано,
            создаётся отдельный ``SDKConfig`` для этого экземпляра.
        parser_registry: Явно собранный instance-local parser registry. Если
            значение не передано, создаётся отдельный пустой registry. Переданный
            объект сохраняется без копирования.

    Создание facade не читает переменные окружения, не создаёт event loop или
    thread и не выполняет файловый либо сетевой I/O. Pipeline пока не реализован,
    поэтому его операции завершаются ``OperationNotImplementedError``.
    Discovery plugins автоматически не запускается. Зарегистрированные вручную
    parsers выполняются in-process и должны быть доверенными.
    """

    __slots__ = ("_config", "_parsers")

    def __init__(
        self,
        *,
        config: SDKConfig | None = None,
        parser_registry: ParserRegistry | None = None,
    ) -> None:
        self._config = config if config is not None else SDKConfig()
        self._parsers = (
            parser_registry if parser_registry is not None else ParserRegistry()
        )

    @property
    def config(self) -> SDKConfig:
        """Вернуть instance-local ``SDKConfig``, переданный при создании."""

        return self._config

    @property
    def parsers(self) -> ParserRegistry:
        """Вернуть тот же instance-local registry technical parsers.

        Registry можно собирать вручную или через явный discovery до открытия
        parser session; во время active session его изменение запрещено.
        """

        return self._parsers

    async def inspect_source(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``inspect_source``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("inspect_source")

    async def inspect_database(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``inspect_database``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("inspect_database")

    async def create_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``create_plan``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("create_plan")

    async def validate_plan(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``validate_plan``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("validate_plan")

    async def execute(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``execute``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("execute")

    async def analyze(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``analyze``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("analyze")

    async def ingest(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``ingest``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("ingest")

    async def propose_schema(self, *args: object, **kwargs: object) -> Never:
        """Отклонить пока не реализованную операцию ``propose_schema``.

        Args:
            *args: Позиционные аргументы, которые пока не интерпретируются.
            **kwargs: Именованные аргументы, которые пока не интерпретируются.

        Raises:
            OperationNotImplementedError: Всегда при ожидании результата.

        Метод не возвращает результат и не выполняет I/O.
        """

        self._operation_unavailable("propose_schema")

    @staticmethod
    def _operation_unavailable(operation: str) -> Never:
        raise OperationNotImplementedError(operation)
