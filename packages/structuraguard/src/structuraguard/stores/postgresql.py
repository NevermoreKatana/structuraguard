"""PostgreSQL staging: отдельные роли bootstrap/write/cleanup и атомарные batches."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from pydantic import Field, SecretStr, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_json_value
from structuraguard.contracts.common import IdentifierStr
from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.staging import (
    StagedBatch,
    StagedRecord,
    StagingLimits,
    StagingRetentionPolicy,
    StagingRun,
)
from structuraguard.database._dry_run_permissions import principal
from structuraguard.database.postgresql import _cleanup, _DriverConnection
from structuraguard.exceptions import LoadError, StagingError

from ._core import Access, StagingOperations, State, bounded, failure
from ._postgresql_schema import check_schema, tables

if TYPE_CHECKING:
    from typing import Self

    from sqlalchemy import Table
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
    from sqlalchemy.sql.elements import ColumnElement


class PostgreSQLStagingTarget(FrozenContract):
    """Trusted credentials/scope отдельной staging роли; constructor не делает I/O.

    Args:
        dsn: SecretStr URL отдельной роли; исключён из repr/JSON.
        principal: Ожидаемые current_user/session_user.
        inspector_principal: Другая роль для анализа БД.
        target_id: Доверенный идентификатор target.
        namespace: Scope приложения, входящий в ключ run.
        schema_name: Заранее согласованная выделенная staging schema.
        purpose: writer для записи, maintenance для cleanup, bootstrap для DDL.

    Raises:
        pydantic.ValidationError: Недопустимая schema, совпадение ролей или поля DTO.

    Только structuraguard_staging или sg_staging_* schema; runtime никогда не
    создаёт schema/tables. principal должен совпасть с login/session user и
    отличаться от inspector_principal. namespace из trusted composition изолирует
    runs приложения; это не замена grants между недоверенными tenants.
    """

    dsn: SecretStr = Field(repr=False, exclude=True)
    principal: IdentifierStr
    inspector_principal: IdentifierStr
    target_id: IdentifierStr
    namespace: IdentifierStr
    schema_name: IdentifierStr = "structuraguard_staging"
    purpose: Literal["writer", "maintenance", "bootstrap"] = "writer"

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        """Отклонить общую с inspector роль и неверную schema при проверке DTO."""
        if not re.fullmatch(
            r"structuraguard_staging|sg_staging_[a-z0-9_]{1,48}", self.schema_name
        ):
            raise ValueError("Требуется выделенная staging schema")
        if self.principal == self.inspector_principal:
            raise ValueError("Staging и inspector требуют разные principals")
        return self


def _target(target: PostgreSQLStagingTarget) -> PostgreSQLStagingTarget:
    if type(target) is not PostgreSQLStagingTarget:
        raise failure("STAGING_TARGET_INVALID")
    try:
        return PostgreSQLStagingTarget.model_validate(
            {**target.model_dump(warnings=False), "dsn": target.dsn}
        )
    except (ValueError, TypeError):
        raise failure("STAGING_TARGET_INVALID") from None


def _engine(target: PostgreSQLStagingTarget, limits: StagingLimits) -> AsyncEngine:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    try:
        url = make_url(target.dsn.get_secret_value())
    except (ArgumentError, ValueError):
        raise failure("STAGING_TARGET_INVALID") from None
    if (
        url.drivername != "postgresql+asyncpg"
        or url.username != target.principal
        or not url.host
        or not url.database
        or set(url.query) - {"ssl"}
    ):
        raise failure("STAGING_TARGET_INVALID")
    engine = create_async_engine(
        url,
        poolclass=NullPool,
        isolation_level="READ COMMITTED",
        echo=False,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": "pg_catalog",
                "statement_timeout": str(limits.timeout_seconds * 1000),
                "lock_timeout": str(limits.timeout_seconds * 1000),
                "application_name": "structuraguard-staging",
            },
            "timeout": limits.timeout_seconds,
            "command_timeout": limits.timeout_seconds + limits.cleanup_seconds,
            "prepared_statement_cache_size": 0,
        },
    )
    quiet = logging.Logger("structuraguard.staging.private", logging.CRITICAL + 1)
    quiet.propagate = False
    engine.sync_engine.logger = quiet
    engine.sync_engine.pool.logger = quiet
    return engine


async def _principal(
    connection: AsyncConnection, target: PostgreSQLStagingTarget
) -> None:
    from sqlalchemy import text

    if target.purpose != "bootstrap":
        try:
            # Та же граница, что у loader: владение любыми DB objects и MEMBER,
            # включая NOINHERIT/SET ROLE, а не только текущие права на таблицы.
            await principal(connection, target.principal, session_mode="writer")
        except LoadError:
            raise failure("STAGING_PRINCIPAL_FORBIDDEN") from None
        return
    row = (await connection.execute(text("SELECT current_user, session_user"))).one()
    if tuple(row) != (target.principal, target.principal):
        raise failure("STAGING_PRINCIPAL_FORBIDDEN")


def _code(error: BaseException) -> str:
    current: object = error
    for _ in range(4):
        state = getattr(current, "sqlstate", None)
        if state in ("42501", "28000", "28P01"):
            return "STAGING_PERMISSION_DENIED"
        if state in ("42P01", "3F000", "42703", "42804"):
            return "STAGING_SCHEMA_UNAVAILABLE"
        if state in ("57014", "55P03"):
            return "PROCESSING_TIMEOUT"
        if state in ("23505", "40001", "40P01"):
            return "STAGING_CONCURRENT_CHANGE"
        current = getattr(current, "orig", None) or getattr(current, "__cause__", None)
    return "STAGING_STORAGE_FAILED"


async def _session[T](
    target: PostgreSQLStagingTarget,
    limits: StagingLimits,
    action: Callable[[AsyncConnection], Awaitable[T]],
) -> T:
    try:
        from asyncpg.exceptions import PostgresError
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError:
        raise failure("STAGING_DEPENDENCY_UNAVAILABLE") from None
    engine: AsyncEngine | None = None
    connection: AsyncConnection | None = None
    driver: _DriverConnection | None = None
    code: str | None = None
    cancelled = False
    commit_started = committed = False
    result: list[T] = []
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            engine = _engine(target, limits)
            connection = await engine.connect()
            raw = await connection.get_raw_connection()
            driver = cast(_DriverConnection, raw.driver_connection)
            await connection.begin()
            await _principal(connection, target)
            result.append(await action(connection))
            commit_started = True
            await connection.commit()
            committed = True
    except asyncio.CancelledError:
        cancelled = True
    except StagingError as error:
        code = error.error_code
    except TimeoutError:
        code = "STAGING_OUTCOME_UNKNOWN" if commit_started else "PROCESSING_TIMEOUT"
    except (SQLAlchemyError, PostgresError, OSError, ValueError, TypeError) as error:
        code = "STAGING_OUTCOME_UNKNOWN" if commit_started else _code(error)
    finally:
        ok, cleanup_cancelled = await _cleanup(
            connection, engine, driver, limits.cleanup_seconds
        )
        cancelled = cancelled or cleanup_cancelled
        if not ok:
            code = (
                "STAGING_POST_COMMIT_CLEANUP_FAILED"
                if committed
                else "STAGING_OUTCOME_UNKNOWN"
            )
    if cancelled:
        raise asyncio.CancelledError
    if code is not None:
        raise failure(code)
    if not result or not committed:
        raise failure("STAGING_STORAGE_FAILED")
    return result[0]


class PostgreSQLStagingStore(StagingOperations):
    """Durable metadata store с одной transaction на вызов и без runtime DDL.

    Args:
        target: Явная writer либо maintenance роль, отличная от inspector/owner.
        retention: Неизменяемая policy хранения refs.
        limits: Конечные budgets; None выбирает defaults.
        clock: Доверенные UTC часы; None использует системные часы.

    Raises:
        StagingError: Неверный target, bootstrap роль, retention или limits.

    Конструктор не выполняет I/O. Методам нужна заранее установленная схема.
    Каждая операция создаёт/закрывает NullPool connection. StagingRunStatus —
    объявление caller, не commit evidence loader. После неизвестного commit
    сверить get_run; повтор stage идемпотентен, transitions защищены revision.
    """

    def __init__(
        self,
        target: PostgreSQLStagingTarget,
        *,
        retention: StagingRetentionPolicy,
        limits: StagingLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = _target(target)
        if target.purpose == "bootstrap":
            raise failure("STAGING_PRINCIPAL_FORBIDDEN")
        super().__init__(
            target_id=target.target_id, retention=retention, limits=limits, clock=clock
        )

    async def _access[T](
        self,
        context: StagingContext,
        mode: Access,
        operation: Callable[[State | None], tuple[State, T]],
    ) -> T:
        if (mode == "cleanup" and self._target.purpose != "maintenance") or (
            mode == "write" and self._target.purpose != "writer"
        ):
            raise failure("STAGING_PRINCIPAL_FORBIDDEN")

        async def action(connection: AsyncConnection) -> T:
            from sqlalchemy import text

            scope = (self._target.namespace, context.target_id, context.run_id)
            key = int.from_bytes(
                hashlib.sha256(canonical_json_value(scope).encode()).digest()[:8],
                signed=True,
            )
            # Все readers/writers одной run используют transaction-scoped lock:
            # это защищает и begin отсутствующей строки, и согласованность страниц.
            await connection.execute(
                text("SELECT pg_catalog.pg_advisory_xact_lock(:key)"), {"key": key}
            )
            await check_schema(connection, self._target.schema_name, mode)
            before = await self._load(connection, context)
            state, result = operation(before)
            if state != before:
                await self._save(connection, context, before, state)
            return result

        return await _session(self._target, self._limits, action)

    def _scope(self, table: Table, context: StagingContext) -> ColumnElement[bool]:
        return (
            (table.c.namespace == self._target.namespace)
            & (table.c.target_id == context.target_id)
            & (table.c.run_id == context.run_id)
        )

    async def _load(
        self, connection: AsyncConnection, context: StagingContext
    ) -> State | None:
        from sqlalchemy import case, func, select

        _, mapping = tables(self._target.schema_name)

        async def read[T: FrozenContract](
            name: str, cls: type[T], maximum: int, order: str
        ) -> tuple[T, ...]:
            table = mapping[name]
            statement = (
                select(
                    case(
                        (
                            func.octet_length(table.c.payload)
                            <= self._limits.max_input_bytes,
                            table.c.payload,
                        ),
                        else_=None,
                    ),
                    func.left(table.c.fingerprint, 65),
                )
                .where(self._scope(table, context))
                .order_by(table.c[order])
                .limit(maximum + 1)
            )
            result: list[T] = []
            used = 0
            async with connection.stream(
                statement, execution_options={"yield_per": 1}
            ) as rows:
                async for payload, fingerprint in rows:
                    if not isinstance(payload, str) or len(result) >= maximum:
                        raise failure("STAGING_LIMIT_EXCEEDED")
                    used += len(payload.encode())
                    if used > self._limits.max_input_bytes:
                        raise failure("STAGING_LIMIT_EXCEEDED")
                    if hashlib.sha256(payload.encode()).hexdigest() != fingerprint:
                        raise failure("STAGING_CONTENT_MISMATCH")
                    try:
                        result.append(cls.model_validate_json(payload))
                    except (ValueError, TypeError):
                        raise failure("STAGING_CONTENT_MISMATCH") from None
            return tuple(result)

        runs = await read("runs", StagingRun, 1, "run_id")
        if not runs:
            return None
        state = State(
            runs[0],
            await read("batches", StagedBatch, self._limits.max_batches, "batch_index"),
            await read(
                "records", StagedRecord, self._limits.max_records, "record_index"
            ),
        )
        bounded((state.run, state.batches, state.records), self._limits)
        if not state.run.purged and (len(state.batches), len(state.records)) != (
            state.run.batch_count,
            state.run.record_count,
        ):
            raise failure("STAGING_CONTENT_MISMATCH")
        return state

    async def _save(
        self,
        connection: AsyncConnection,
        context: StagingContext,
        before: State | None,
        state: State,
    ) -> None:
        from sqlalchemy import delete, insert, update

        _, mapping = tables(self._target.schema_name)
        scope = {
            "namespace": self._target.namespace,
            "target_id": context.target_id,
            "run_id": context.run_id,
        }

        def payload(value: FrozenContract) -> dict[str, object]:
            encoded = value.model_dump_json()
            return {
                "payload": encoded,
                "fingerprint": hashlib.sha256(encoded.encode()).hexdigest(),
            }

        if before is None:
            await connection.execute(
                insert(mapping["runs"]), {**scope, **payload(state.run)}
            )
        else:
            await connection.execute(
                update(mapping["runs"]).where(self._scope(mapping["runs"], context)),
                payload(state.run),
            )
        if state.run.purged:
            for name in ("records", "batches"):
                await connection.execute(
                    delete(mapping[name]).where(self._scope(mapping[name], context))
                )
            return
        batches = state.batches[len(before.batches) if before else 0 :]
        records = state.records[len(before.records) if before else 0 :]
        if batches:
            await connection.execute(
                insert(mapping["batches"]),
                [
                    {**scope, "batch_index": b.summary.batch_index, **payload(b)}
                    for b in batches
                ],
            )
        if records:
            await connection.execute(
                insert(mapping["records"]),
                [
                    {
                        **scope,
                        "record_index": r.record_index,
                        "record_id": r.record_id,
                        **payload(r),
                    }
                    for r in records
                ],
            )
