"""PostgreSQL metadata adapter: отдельный asyncpg connection, SQLAlchemy 2.x."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import ValidationError

from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    DatabaseMetadataSnapshot,
    LoadContext,
)
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import LoadReport
from structuraguard.exceptions import (
    DatabaseInspectionError,
    OperationNotImplementedError,
)

from . import _postgresql_queries as queries
from ._catalog import inspect_catalog
from ._inspection import failure, inspection_slot
from ._postgresql_catalog import PostgreSQLReader, integer, reflect, string
from .target import PostgreSQLTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class _DriverConnection(Protocol):
    def terminate(self) -> None: ...


class PostgreSQLDatabaseAdapter:
    """Получать каталог PostgreSQL с отдельными credentials inspector.

    Args:
        target: Доверенный inspector DSN, точные schema/table selectors и limits.
            Конфигурация повторно валидируется; writer connection не принимается.

    Raises:
        pydantic.ValidationError: Некорректные поля target; проверка DSN/driver
            выполняется при inspection до открытия соединения.

    Side effects:
        Конструктор не выполняет I/O. Каждый inspection создаёт отдельное
        соединение NullPool; один экземпляр допускает один активный вызов.

    Security:
        Inspector и writer должны быть разными principals. Metadata читаются
        в SERIALIZABLE READ ONLY transaction без DDL/DML, user rows и LLM calls.
        Каталог не подтверждает права writer; DSN не входит в request/результат.
    """

    def __init__(self, target: PostgreSQLTarget) -> None:
        self._target = PostgreSQLTarget.model_validate(
            {**target.model_dump(), "dsn": target.dsn}
        )
        self._active = threading.Lock()
        self._unavailable = False

    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        """Получить каталог с fingerprint и FK graph после cleanup соединения.

        Args:
            request: Target ID, ожидаемый policy fingerprint и сужение schemas.

        Returns:
            DatabaseCatalog schema 1.1.0 с ``catalog-v1``; циклы дают
            ``load_order=None``. Fingerprint покрывает представимые metadata.

        Raises:
            DatabaseInspectionError: Нарушение policy, DSN/permission error,
                missing/unsupported metadata, limits, timeout или cleanup error.
                Код ``DATABASE_PERMISSION_DENIED`` включает authentication failure.
            asyncio.CancelledError: Отмена после ограниченного cleanup.

        Side effects:
            Подключается через asyncpg/SQLAlchemy к trusted endpoint, читает
            только pg_catalog и закрывает соединение. Общий deadline учитывает
            reflection, cleanup и сборку полного каталога.

        Security:
            Scope проверяется до I/O, FK не расширяет allowlist. Нет partial
            catalog, DDL/DML и исполнения expressions; raw errors/DSN скрыты.
            После cleanup failure экземпляр блокирует новые вызовы.
        """
        with inspection_slot(self._active, unavailable=self._unavailable):
            try:
                return await inspect_catalog(
                    lambda: self._inspect_metadata(request), self._target.limits
                )
            except DatabaseInspectionError as error:
                if error.error_code == "DATABASE_INSPECTION_CLEANUP_FAILED":
                    self._unavailable = True
                raise

    async def inspect_metadata(
        self, request: DatabaseInspectionRequest
    ) -> DatabaseMetadataSnapshot:
        """Получить metadata snapshot без fingerprint и dependency graph.

        Args:
            request: Target/policy binding и необязательное сужение schemas.

        Returns:
            DatabaseMetadataSnapshot schema 1.1.0 с замкнутыми FK-ссылками,
            ``comments_supported=True`` и ``write_permissions="unknown"``.

        Raises:
            DatabaseInspectionError: Policy, DSN/permission, metadata, limits
                или cleanup error, как у ``inspect``; без partial snapshot.
            asyncio.CancelledError: Отмена после ограниченного cleanup.

        Side effects:
            Открывает отдельное read-only соединение и закрывает до возврата.
            Metadata expressions не исполняются, user rows не выбираются.
            Comments остаются недоверенными данными; DSN/driver errors скрыты.
        """
        with inspection_slot(self._active, unavailable=self._unavailable):
            return await self._inspect_metadata(request)

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: ValidatedMappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        """Отклонить загрузку: PostgreSQL inspection adapter не реализует writer.

        Args:
            batches: Поток для совместимости с port; не читается.
            plan: План загрузки; не исполняется.
            context: Контекст загрузки; не используется для открытия БД.

        Raises:
            OperationNotImplementedError: Всегда, ``SDK_OPERATION_NOT_IMPLEMENTED``.

        Side effects:
            Нет I/O, DDL/DML или потребления batches; LoadReport не возвращается.
        """
        raise OperationNotImplementedError("database.execute")

    async def _inspect_metadata(
        self, request: DatabaseInspectionRequest
    ) -> DatabaseMetadataSnapshot:
        """Вернуть полный snapshot после rollback и закрытия inspection connection.

        Raises:
            DatabaseInspectionError: Policy, permission, timeout, resource limit
                или unsupported metadata; сообщение не содержит driver/SQL/DSN.
            asyncio.CancelledError: Отмена caller после ограниченного cleanup.

        Side effects:
            Read-only transaction читает pg_catalog; пользовательские rows,
            views и metadata expressions не исполняются.
        """
        names, schemas = self._scope(request)
        return await self._inspect(names, schemas)

    def _scope(
        self, request: DatabaseInspectionRequest
    ) -> tuple[tuple[tuple[str, str], ...], frozenset[str]]:
        try:
            checked = DatabaseInspectionRequest.model_validate(request.model_dump())
        except ValidationError:
            checked = None
        if checked is None:
            raise failure("TARGET_NOT_ALLOWED")
        target = self._target
        if checked.target_id != target.target_id:
            raise failure("DATABASE_TARGET_MISMATCH")
        if checked.target_policy_fingerprint != target.policy_fingerprint:
            raise failure("DATABASE_POLICY_MISMATCH")
        if target.include_columns or target.deny_columns:
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        allowed = set(target.include_schemas) - set(target.deny_schemas)
        if any(s == "information_schema" or s.startswith("pg_") for s in allowed):
            raise failure("TARGET_NOT_ALLOWED")
        if not set(checked.schema_names) <= allowed:
            raise failure("TARGET_NOT_ALLOWED")
        selected = set(checked.schema_names) if checked.schema_names else allowed
        names = tuple(
            sorted(
                name
                for name in target.include_tables
                if name[0] in selected and name not in target.deny_tables
            )
        )
        if not names:
            raise failure("TARGET_NOT_ALLOWED")
        if len(names) > target.limits.max_tables:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        return names, frozenset(selected)

    def _engine(self) -> AsyncEngine:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        target = self._target
        try:
            url = make_url(target.dsn.get_secret_value())
        except (ArgumentError, ValueError):
            raise failure("DATABASE_TARGET_INVALID") from None
        if (
            url.drivername != "postgresql+asyncpg"
            or set(url.query) - {"ssl"}
            or not url.host
            or not url.username
            or not url.database
        ):
            raise failure("DATABASE_TARGET_INVALID")
        limits = target.limits
        # Startup settings защищают также bootstrap-запросы SQLAlchemy.
        settings = {
            "default_transaction_read_only": "on",
            "default_transaction_isolation": "serializable",
            "statement_timeout": str(
                max(1, int(limits.statement_timeout_seconds * 1000))
            ),
            "lock_timeout": str(max(1, int(limits.lock_timeout_seconds * 1000))),
            "search_path": "pg_catalog",
            "application_name": "structuraguard-inspection",
        }
        engine = create_async_engine(
            url,
            poolclass=NullPool,
            isolation_level="SERIALIZABLE",
            execution_options={"postgresql_readonly": True},
            echo=False,
            hide_parameters=True,
            connect_args={
                "server_settings": settings,
                "timeout": min(
                    limits.statement_timeout_seconds, limits.timeout_seconds
                ),
                "command_timeout": limits.statement_timeout_seconds
                + limits.cleanup_seconds,
                "prepared_statement_cache_size": 0,
            },
        )
        # echo=False не блокирует DEBUG result rows в application logger.
        quiet = logging.Logger(
            "structuraguard.inspection.private", logging.CRITICAL + 1
        )
        quiet.propagate = False
        engine.sync_engine.logger = quiet
        engine.sync_engine.pool.logger = quiet
        return engine

    async def _inspect(
        self, names: tuple[tuple[str, str], ...], schemas: frozenset[str]
    ) -> DatabaseMetadataSnapshot:
        try:
            from asyncpg.exceptions import PostgresError
            from sqlalchemy.exc import SQLAlchemyError
        except ImportError:
            raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None

        limits = self._target.limits
        engine: AsyncEngine | None = None
        connection: AsyncConnection | None = None
        driver: _DriverConnection | None = None
        result: DatabaseMetadataSnapshot | None = None
        error_code: str | None = None
        cancelled = False
        try:
            async with asyncio.timeout(limits.timeout_seconds):
                engine = self._engine()
                connection = await engine.connect()
                raw = await connection.get_raw_connection()
                driver = cast(_DriverConnection, raw.driver_connection)
                await connection.begin()
                reader = PostgreSQLReader(connection, self._target, schemas)
                status = (await reader.rows(queries.STATE, {}, 1))[0]
                if (
                    string(status, "read_only") != "on"
                    or string(status, "isolation") != "serializable"
                ):
                    raise failure("DATABASE_READ_ONLY_REQUIRED")
                if not 150000 <= integer(status, "version") < 190000:
                    raise failure("DATABASE_METADATA_UNSUPPORTED")
                reader.server_version = integer(status, "version")
                result = await reflect(reader, names)
                if (
                    len(result.model_dump_json().encode("utf-8"))
                    > limits.max_metadata_bytes
                ):
                    raise failure("SECURITY_LIMIT_EXCEEDED")
        except asyncio.CancelledError:
            cancelled = True
        except DatabaseInspectionError as error:
            error_code = error.error_code
        except TimeoutError:
            error_code = "PROCESSING_TIMEOUT"
        except (SQLAlchemyError, PostgresError, OSError) as error:
            error_code = _error_code(error)
        except (ValidationError, ValueError, KeyError, TypeError):
            error_code = "DATABASE_METADATA_UNSUPPORTED"
        finally:
            cleanup_ok, cleanup_cancelled = await _cleanup(
                connection, engine, driver, limits.cleanup_seconds
            )
            cancelled = cancelled or cleanup_cancelled
            if not cleanup_ok:
                self._unavailable = True
                error_code = "DATABASE_INSPECTION_CLEANUP_FAILED"
        # Выход из except удаляет исходный credential-bearing exception context.
        if cancelled:
            raise asyncio.CancelledError
        if error_code is not None:
            raise failure(error_code)
        if result is None:
            raise failure("DATABASE_INSPECTION_FAILED")
        return result


def _error_code(error: BaseException) -> str:
    current: object = error
    for _ in range(3):
        state = getattr(current, "sqlstate", None)
        if state in {"42501", "28000", "28P01"}:
            return "DATABASE_PERMISSION_DENIED"
        if state in {"57014", "55P03"}:
            return "PROCESSING_TIMEOUT"
        current = getattr(current, "orig", None)
        if current is None:
            break
    return "DATABASE_INSPECTION_FAILED"


async def _cleanup(
    connection: AsyncConnection | None,
    engine: AsyncEngine | None,
    driver: _DriverConnection | None,
    seconds: float,
) -> tuple[bool, bool]:
    from sqlalchemy.exc import SQLAlchemyError

    async def close() -> None:
        try:
            if connection is not None:
                await connection.close()
        finally:
            if engine is not None:
                await engine.dispose()

    task = asyncio.create_task(close())
    deadline = asyncio.get_running_loop().time() + seconds
    cancelled = False
    while True:
        try:
            async with asyncio.timeout_at(deadline):
                # Поздняя ошибка driver остаётся здесь: отменённый shield
                # в Python 3.14 передаёт её несаницированной в event loop.
                await asyncio.wait((task,))
                task.result()
            return True, cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                return False, cancelled
        except (TimeoutError, SQLAlchemyError, OSError):
            if driver is not None:
                driver.terminate()
            if connection is not None and not connection.closed:
                # Transport уже закрыт: invalidation/check-in не требует await
                # и освобождает record даже при зависшем async close.
                synchronous = connection.sync_connection
                if synchronous is not None:
                    synchronous.invalidate()
                    synchronous.close()
            task.cancel()
            task.add_done_callback(_consume_cleanup)
            return False, cancelled


def _consume_cleanup(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()
