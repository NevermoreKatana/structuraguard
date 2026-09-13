"""Настоящий PostgreSQL dry-run без writer handle, DML или persistent staging."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, cast

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
)
from structuraguard.exceptions import StructuraGuardError
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import Prepared, checked, failure, prepare

from . import _postgresql_queries as queries
from ._catalog import inspect_catalog
from ._dry_run_permissions import permissions
from ._dry_run_reader import SnapshotReader
from ._inspection import inspection_slot
from ._postgresql_catalog import PostgreSQLReader, integer, reflect, string
from .postgresql import (
    PostgreSQLDatabaseAdapter,
    _cleanup,
    _DriverConnection,
    _error_code,
)
from .target import PostgreSQLTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class PostgreSQLDryRunPlanner:
    """Планировать загрузку, не меняя target/staging даже временно.

    Args:
        target: Trusted inspector endpoint, exact table scope и конечные budgets.
        policy: Отдельный writer login для проверки grants, M11 и SELECT allowlist.

    Raises:
        DatabaseInspectionError: Неверная конфигурация inspector.
        LoadError: Неверная policy или превышение её бюджета.

    Конструктор не выполняет I/O. Writer credentials, staging/audit adapters и SQL
    от caller не принимаются. Один экземпляр обслуживает один активный вызов.
    """

    def __init__(self, target: PostgreSQLTarget, *, policy: DryRunPolicy) -> None:
        self._adapter = PostgreSQLDatabaseAdapter(target)
        self._target = self._adapter._target
        self._policy = checked(policy, DryRunPolicy, 4194304)
        self._active = threading.Lock()
        self._unavailable = False

    async def plan(self, request: DryRunRequest) -> DryRunExecutionPlan:
        """Построить ordered plan в одной SERIALIZABLE READ ONLY transaction.

        Args:
            request: Полный NormalizedBatch 1.1/1.2 snapshot с EOF manifest и MappingPlan.

        Returns:
            DryRunExecutionPlan после rollback/close с table order, решениями по units
            и blockers. ready относится только к поддержанным проверкам этого snapshot.

        Raises:
            DatabaseInspectionError: Экземпляр занят либо заблокирован после
                предыдущего сбоя cleanup.
            LoadError: Невалидный snapshot, drift, M11 rejection, grants, timeout
                или DB/cleanup error; закрытый code не содержит SQL/parameters/DSN.
            asyncio.CancelledError: Отмена после ограниченного rollback/close.

        INSERT/UPDATE counts — прогноз, без резервирования keys. Неизвестные
        CHECK/defaults, key semantics и provenance блокируют ready. Target/staging
        DML, nextval, DDL и audit append не выполняются. Сбой cleanup блокирует
        повторное использование экземпляра; для logs используйте safe_summary().
        """
        with inspection_slot(self._active, unavailable=self._unavailable):
            try:
                async with asyncio.timeout(self._target.limits.timeout_seconds):
                    prepared = await prepare(
                        request,
                        max_bytes=self._policy.read_policy.limits.max_bytes,
                        max_records=self._policy.read_policy.limits.max_records,
                    )
                    return await self._plan(prepared)
            except StructuraGuardError as error:
                code = error.error_code
            except TimeoutError:
                code = "PROCESSING_TIMEOUT"
        raise failure(code) from None

    async def _plan(self, prepared: Prepared) -> DryRunExecutionPlan:
        try:
            from asyncpg.exceptions import PostgresError
            from sqlalchemy.exc import SQLAlchemyError
        except ImportError:
            raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None
        target = self._target
        mapping = prepared.request.mapping
        names, schemas = self._adapter._scope(
            DatabaseInspectionRequest(
                target_id=mapping.target_id,
                target_policy_fingerprint=mapping.target_policy_fingerprint,
            )
        )
        engine: AsyncEngine | None = None
        connection: AsyncConnection | None = None
        driver: _DriverConnection | None = None
        result: DryRunExecutionPlan | None = None
        code: str | None = None
        cancelled = False
        try:
            engine = self._adapter._engine()
            connection = await engine.connect()
            raw = await connection.get_raw_connection()
            driver = cast(_DriverConnection, raw.driver_connection)
            await connection.begin()
            quote = connection.dialect.identifier_preparer.quote_identifier
            for schema, name in names:
                sql = f"LOCK TABLE ONLY {quote(schema)}.{quote(name)} IN ACCESS SHARE MODE"
                if len(sql.encode("utf-8")) > target.limits.max_sql_bytes:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                await connection.exec_driver_sql(sql)
            metadata = PostgreSQLReader(connection, target, schemas)
            state = (await metadata.rows(queries.STATE, {}, 1))[0]
            if (
                string(state, "read_only") != "on"
                or string(state, "isolation") != "serializable"
            ):
                raise failure("DATABASE_READ_ONLY_REQUIRED")
            metadata.server_version = integer(state, "version")
            if not 150000 <= metadata.server_version < 190000:
                raise failure("DATABASE_METADATA_UNSUPPORTED")
            catalog = await inspect_catalog(
                lambda: reflect(metadata, names), target.limits
            )
            if catalog.database_fingerprint != mapping.database_fingerprint:
                raise failure("DATABASE_SCHEMA_DRIFT")
            await permissions(connection, catalog, self._policy, mapping)
            result = await build_plan(
                prepared,
                catalog=catalog,
                policy=self._policy,
                reader=SnapshotReader(connection, self._policy.read_policy),
            )
        except asyncio.CancelledError:
            cancelled = True
        except StructuraGuardError as error:
            code = error.error_code
        except (SQLAlchemyError, PostgresError, OSError) as error:
            known = _error_code(error)
            code = {
                "DATABASE_PERMISSION_DENIED": "DRY_RUN_PERMISSION_DENIED",
                "PROCESSING_TIMEOUT": "PROCESSING_TIMEOUT",
            }.get(known, "DRY_RUN_DATABASE_READ_FAILED")
        except (ValueError, TypeError):
            code = "DRY_RUN_DATABASE_READ_FAILED"
        finally:
            cleanup_ok, cleanup_cancelled = await _cleanup(
                connection, engine, driver, target.limits.cleanup_seconds
            )
            cancelled = cancelled or cleanup_cancelled
            if not cleanup_ok:
                self._unavailable = True
                code = "DATABASE_INSPECTION_CLEANUP_FAILED"
        if cancelled:
            raise asyncio.CancelledError
        if code is not None:
            raise failure(code)
        if result is None:
            raise failure("DRY_RUN_DATABASE_READ_FAILED")
        return result
