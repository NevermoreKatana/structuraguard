"""SQLite/PostgreSQL adapter закрытого read-only ConstraintReader port."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from typing import TYPE_CHECKING, cast

from structuraguard.contracts.constraint_validation import (
    ConstraintMatch,
    ConstraintReadPolicy,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    DatabaseMetadataSnapshot,
    SchemaCatalog,
)
from structuraguard.contracts.record_validation import RecordValidationLimits
from structuraguard.domain.database_fingerprint import verify_database_fingerprint
from structuraguard.exceptions import DatabaseInspectionError
from structuraguard.validation._rule_input import checked

from . import _constraint_queries as key_queries
from . import _postgresql_queries as metadata_queries
from ._inspection import InspectionControl, failure, inspection_slot, run_inspection
from ._postgresql_catalog import PostgreSQLReader, integer, reflect, string
from .normalization import catalog_identifier
from .postgresql import PostgreSQLDatabaseAdapter, _cleanup, _DriverConnection
from .sqlite import _authorize, _resolve_foreign_keys, _SQLiteReader
from .target import PostgreSQLTarget, SQLiteTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class DatabaseConstraintReader:
    """Read-only SQLite/PostgreSQL EXISTS с повторной инспекцией в той же transaction.

    Args:
        target: Trusted endpoint с ограниченным DB principal и table allowlist.
        policy: Отдельное разрешение читать конкретные key columns и budgets.

    Конструктор не выполняет I/O. Нет writer handle, SQL/path из requests,
    materialization строк или cache; каждый read закрывает отдельное соединение.
    Лимиты target ограничивают statements, общий deadline и cleanup. Результат
    advisory: snapshot заканчивается до будущей записи. RLS отклоняется.
    """

    def __init__(
        self, target: SQLiteTarget | PostgreSQLTarget, *, policy: ConstraintReadPolicy
    ) -> None:
        if type(target) is SQLiteTarget:
            self._target: SQLiteTarget | PostgreSQLTarget = SQLiteTarget.model_validate(
                target.model_dump()
            )
        elif type(target) is PostgreSQLTarget:
            self._target = PostgreSQLTarget.model_validate(
                {**target.model_dump(), "dsn": target.dsn}
            )
        else:
            raise failure("DATABASE_TARGET_INVALID")
        self._policy = checked(
            policy,
            ConstraintReadPolicy,
            RecordValidationLimits(),
            code="DB_READER_POLICY_INVALID",
        )
        self._active = threading.Lock()
        self._unavailable = False

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        """Вернуть boolean matches из одного read-only snapshot, без выдачи строк.

        Args:
            request: Bounded keys и fingerprints, связанные с доверенным target.
            catalog: Catalog с разрешёнными table/column IDs и ожидаемой схемой БД.

        Returns:
            ConstraintReadResult с matches в порядке request и snapshot fingerprint.

        Raises:
            ValidationError: Невалидная форма request/catalog либо intake budget.
            DatabaseInspectionError: Запрещённый scope, schema drift, отказ БД,
                timeout или cleanup failure; частичный результат не возвращается.

        Открывает и закрывает отдельное соединение, повторяет инспекцию и выполняет
        параметризованные prechecks. DDL/запись и SQL из request запрещены.
        Cancellation распространяется после cleanup; snapshot не защищает будущую
        запись от TOCTOU. Полные requests/keys чувствительны и не подходят для logs.
        """
        request = checked(
            request,
            ConstraintReadRequest,
            self._policy.limits,
            code="DB_READER_INPUT_INVALID",
        )
        catalog = checked(
            catalog,
            DatabaseCatalog,
            self._policy.limits,
            code="DB_READER_INPUT_INVALID",
        )
        target = self._target
        if (
            request.target_id != target.target_id
            or request.target_id != catalog.target_id
        ):
            raise failure("DATABASE_TARGET_MISMATCH")
        if (
            request.target_policy_fingerprint != target.policy_fingerprint
            or request.target_policy_fingerprint != catalog.target_policy_fingerprint
        ):
            raise failure("DATABASE_POLICY_MISMATCH")
        if catalog.database_fingerprint != request.database_fingerprint:
            raise failure("DATABASE_SCHEMA_DRIFT")
        verify_database_fingerprint(catalog, request.database_fingerprint)
        if (
            len(request.lookups) > self._policy.limits.max_keys
            or (len(request.lookups) + self._policy.chunk_size - 1)
            // self._policy.chunk_size
            > self._policy.max_queries
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")
        # Scope и формы keys проверяются до первого I/O; после reflection — снова.
        snapshot = DatabaseMetadataSnapshot(
            dialect=catalog.dialect,
            target_id=catalog.target_id,
            target_policy_fingerprint=catalog.target_policy_fingerprint,
            schemas=catalog.schemas,
            comments_supported=catalog.comments_supported is True,
        )
        key_queries.checked_tables(snapshot, request, self._policy)
        if target.include_columns or target.deny_columns:
            raise failure("TARGET_NOT_ALLOWED")
        self._scope(catalog)
        with inspection_slot(self._active, unavailable=self._unavailable):
            try:
                if isinstance(target, SQLiteTarget):
                    return await run_inspection(
                        lambda control: self._sqlite(request, target, control),
                        target.limits,
                    )
                return await self._postgresql(request, target, catalog)
            except DatabaseInspectionError as error:
                if error.error_code == "DATABASE_INSPECTION_CLEANUP_FAILED":
                    self._unavailable = True
                raise

    def _scope(self, catalog: DatabaseCatalog) -> None:
        target = self._target
        if isinstance(target, SQLiteTarget):
            allowed = {
                ("main", n)
                for n in target.include_tables
                if n.lower() not in {d.lower() for d in target.deny_tables}
            }
            if (
                target.include_schemas != ("main",)
                or any(n.lower() == "main" for n in target.deny_schemas)
                or catalog.dialect != "sqlite"
            ):
                raise failure("TARGET_NOT_ALLOWED")
        else:
            allowed = {
                pair
                for pair in target.include_tables
                if pair[0] in target.include_schemas
                and pair[0] not in target.deny_schemas
                and pair not in target.deny_tables
            }
            if catalog.dialect != "postgresql":
                raise failure("TARGET_NOT_ALLOWED")
        names = {(t.schema_name, t.name) for s in catalog.schemas for t in s.tables}
        if len(names) > target.limits.max_tables:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if (
            not allowed
            or names != allowed
            or any(
                s == "information_schema"
                or s.startswith("pg_")
                or n.lower().startswith("sqlite_")
                for s, n in names
            )
        ):
            raise failure("TARGET_NOT_ALLOWED")

    def _sqlite(
        self,
        request: ConstraintReadRequest,
        target: SQLiteTarget,
        control: InspectionControl,
    ) -> ConstraintReadResult:
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.exc import SQLAlchemyError
            from sqlalchemy.pool import NullPool
        except ImportError:
            raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None

        permitted: set[tuple[str, str]] = set()

        def authorize(
            action: int,
            arg1: str | None,
            arg2: str | None,
            database: str | None,
            source: str | None,
        ) -> int:
            if source is not None:
                return sqlite3.SQLITE_DENY
            if (
                action == sqlite3.SQLITE_READ
                and database == "main"
                and (arg1, arg2) in permitted
            ):
                return sqlite3.SQLITE_OK
            return _authorize(action, arg1, arg2, database, source)

        def connect() -> sqlite3.Connection:
            control.check()
            raw = sqlite3.connect(
                target.path.as_uri() + "?mode=ro",
                uri=True,
                isolation_level=None,
                timeout=target.limits.lock_timeout_seconds,
            )
            try:
                raw.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, control.limits.max_sql_bytes)
                raw.setlimit(
                    sqlite3.SQLITE_LIMIT_SQL_LENGTH, control.limits.max_sql_bytes
                )
                raw.execute("PRAGMA trusted_schema = OFF")
                raw.execute("PRAGMA query_only = ON")
                if raw.execute("PRAGMA query_only").fetchone() != (1,):
                    raise failure("DATABASE_READ_ONLY_REQUIRED")
                raw.set_progress_handler(lambda: int(control.expired()), 100)
                raw.set_authorizer(authorize)
                control.attach(raw)
            except (sqlite3.Error, DatabaseInspectionError):
                raw.close()
                raise
            return raw

        engine = create_engine(
            "sqlite+pysqlite://",
            creator=connect,
            poolclass=NullPool,
            echo=False,
            hide_parameters=True,
        )
        logger = logging.Logger(
            "structuraguard.constraints.private", logging.CRITICAL + 1
        )
        logger.propagate = False
        engine.logger = engine.pool.logger = logger
        code = "DB_CONSTRAINT_READ_FAILED"
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN")
                metadata_reader = _SQLiteReader(connection, control)
                names = sorted(
                    n
                    for n in target.include_tables
                    if n.lower() not in {d.lower() for d in target.deny_tables}
                )
                tables = _resolve_foreign_keys(
                    tuple(metadata_reader.table(n) for n in names),
                    metadata_reader.references,
                )
                snapshot = DatabaseMetadataSnapshot(
                    dialect="sqlite",
                    target_id=target.target_id,
                    target_policy_fingerprint=target.policy_fingerprint,
                    comments_supported=False,
                    schemas=(
                        SchemaCatalog(
                            schema_id=catalog_identifier("schema", "main"),
                            name="main",
                            tables=tables,
                        ),
                    ),
                )
                by_id = key_queries.checked_tables(snapshot, request, self._policy)
                for lookup in request.lookups:
                    table = by_id[lookup.table_id]
                    columns = {c.column_id: c.name for c in table.columns}
                    permitted.update(
                        (table.name, columns[c])
                        for c in (*lookup.column_ids, *lookup.identity_column_ids)
                    )
                outcomes: list[ConstraintMatch] = []
                for chunk, statement in key_queries.statements(
                    request, by_id, self._policy.chunk_size, "sqlite"
                ):
                    control.check()
                    # Общий progress deadline ограничивает также costly EXISTS.
                    control.statement_deadline = min(
                        control.deadline,
                        time.monotonic() + control.limits.statement_timeout_seconds,
                    )
                    with connection.execute(statement) as cursor:
                        values = tuple(cursor.one())
                    control.check()
                    control.statement_deadline = None
                    control.account(values)
                    outcomes.extend(key_queries.matches(chunk, values))
                connection.rollback()
                control.check()
                return key_queries.result(request, tuple(outcomes))
        except (SQLAlchemyError, sqlite3.Error, OSError, ValueError):
            code = (
                "PROCESSING_TIMEOUT"
                if control.expired()
                else "DB_CONSTRAINT_READ_FAILED"
            )
        finally:
            control.close()
            engine.dispose()
        raise failure(code)

    async def _postgresql(
        self,
        request: ConstraintReadRequest,
        target: PostgreSQLTarget,
        catalog: DatabaseCatalog,
    ) -> ConstraintReadResult:
        try:
            from asyncpg.exceptions import PostgresError
            from sqlalchemy import text
            from sqlalchemy.exc import SQLAlchemyError
        except ImportError:
            raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None

        adapter = PostgreSQLDatabaseAdapter(target)
        names, schemas = adapter._scope(
            DatabaseInspectionRequest(
                target_id=target.target_id,
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
        engine: AsyncEngine | None = None
        connection: AsyncConnection | None = None
        driver: _DriverConnection | None = None
        result: ConstraintReadResult | None = None
        code: str | None = None
        cancelled = False
        try:
            async with asyncio.timeout(target.limits.timeout_seconds):
                engine = adapter._engine()
                connection = await engine.connect()
                raw = await connection.get_raw_connection()
                driver = cast(_DriverConnection, raw.driver_connection)
                await connection.begin()
                # LOCK предшествует первому SELECT/snapshot: имя не сможет стать
                # view между reflection и EXISTS. AccessShare не блокирует DML.
                selected_ids = {key.table_id for key in request.lookups}
                selected_names = sorted(
                    (table.schema_name, table.name)
                    for schema in catalog.schemas
                    for table in schema.tables
                    if table.table_id in selected_ids
                )
                quote = connection.dialect.identifier_preparer.quote_identifier
                for schema_name, table_name in selected_names:
                    sql = (
                        f"LOCK TABLE ONLY {quote(schema_name)}.{quote(table_name)} "
                        "IN ACCESS SHARE MODE"
                    )
                    if len(sql.encode("utf-8")) > target.limits.max_sql_bytes:
                        raise failure("SECURITY_LIMIT_EXCEEDED")
                    await connection.exec_driver_sql(sql)
                metadata_reader = PostgreSQLReader(connection, target, schemas)
                status = (await metadata_reader.rows(metadata_queries.STATE, {}, 1))[0]
                if (
                    string(status, "read_only") != "on"
                    or string(status, "isolation") != "serializable"
                ):
                    raise failure("DATABASE_READ_ONLY_REQUIRED")
                metadata_reader.server_version = integer(status, "version")
                if not 150000 <= metadata_reader.server_version < 190000:
                    raise failure("DATABASE_METADATA_UNSUPPORTED")
                snapshot = await reflect(metadata_reader, names)
                by_id = key_queries.checked_tables(snapshot, request, self._policy)
                selected = {key.table_id for key in request.lookups}
                # RLS может скрывать конфликтующие keys даже при успешном SELECT.
                for tid in sorted(selected):
                    table = by_id[tid]
                    status_row = (
                        await connection.execute(
                            text(
                                "SELECT c.relrowsecurity FROM pg_catalog.pg_class AS c JOIN pg_catalog.pg_namespace AS n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:table"
                            ),
                            {"schema": table.schema_name, "table": table.name},
                        )
                    ).one()
                    if status_row[0] is not False:
                        raise failure("DB_CONSTRAINT_UNVERIFIED")
                outcomes: list[ConstraintMatch] = []
                for chunk, statement in key_queries.statements(
                    request, by_id, self._policy.chunk_size, "postgresql"
                ):
                    cursor = await connection.execute(statement)
                    outcomes.extend(key_queries.matches(chunk, tuple(cursor.one())))
                    cursor.close()
                result = key_queries.result(request, tuple(outcomes))
        except asyncio.CancelledError:
            cancelled = True
        except DatabaseInspectionError as error:
            code = error.error_code
        except TimeoutError:
            code = "PROCESSING_TIMEOUT"
        except (SQLAlchemyError, PostgresError, OSError, ValueError, TypeError):
            code = "DB_CONSTRAINT_READ_FAILED"
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
            raise failure("DB_CONSTRAINT_READ_FAILED")
        return result
