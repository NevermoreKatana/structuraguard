"""Отдельный writer endpoint с тем же trusted scope, без import-time I/O."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import Field, SecretStr

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import IdentifierStr
from structuraguard.loading.projection import failure

from .target import PostgreSQLTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


class PostgreSQLWriterTarget(FrozenContract):
    """Отдельные writer credentials для того же endpoint и scope, что у inspector.

    Args:
        inspection: Доверенный PostgreSQLTarget с scope и конечными budgets.
        dsn: SecretStr URL postgresql+asyncpg с writer credentials; исключён из JSON.
        principal: Ожидаемые current_user/session_user writer, отличные от inspector.

    Конструктор Pydantic проверяет форму без I/O; совпадение host/port/database,
    разные logins и реальные grants проверяются при операции. Конфигурация
    принадлежит приложению, не поступает из MappingPlan или LLM. Не выдаёт DDL
    privileges; SQLAlchemy/asyncpg загружаются только при выполнении операции.
    """

    inspection: PostgreSQLTarget = Field(repr=False, exclude=True)
    dsn: SecretStr = Field(repr=False, exclude=True)
    principal: IdentifierStr


def writer_scope(target: PostgreSQLWriterTarget) -> PostgreSQLTarget:
    """Построить writer scope с endpoint и allowlist исходного inspector.

    Args:
        target: Доверенная конфигурация разных inspector/writer logins.

    Returns:
        PostgreSQLTarget с writer DSN и сохранёнными inspection limits/scope.

    Raises:
        LoadError: Недоступен DB extra, неверный DSN или endpoint/login mismatch.

    Загружает SQLAlchemy при вызове, но не открывает соединение. Credentials
    остаются SecretStr; произвольные URL options кроме ssl запрещены.
    """
    try:
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import ArgumentError
    except ImportError:
        raise failure("DATABASE_DEPENDENCY_UNAVAILABLE") from None

    try:
        write = make_url(target.dsn.get_secret_value())
        read = make_url(target.inspection.dsn.get_secret_value())
        policy = target.inspection.security_policy
        if policy is not None and (
            read.username != policy.inspector_principal
            or write.username != policy.writer_principal
            or target.principal != policy.writer_principal
        ):
            raise failure("LOAD_WRITER_TARGET_INVALID")
        if (
            any(
                u.drivername != "postgresql+asyncpg"
                or not u.host
                or not u.username
                or not u.database
                or set(u.query) - {"ssl"}
                for u in (write, read)
            )
            or (write.host, write.port or 5432, write.database)
            != (read.host, read.port or 5432, read.database)
            or write.username != target.principal
            or write.username == read.username
        ):
            raise failure("LOAD_WRITER_TARGET_INVALID")
        return PostgreSQLTarget.model_validate(
            {**target.inspection.model_dump(), "dsn": target.dsn}
        )
    except (ArgumentError, ValueError, TypeError):
        raise failure("LOAD_WRITER_TARGET_INVALID") from None


def writer_engine(target: PostgreSQLTarget) -> AsyncEngine:
    """Подготовить owned engine для уже проверенного writer scope.

    Args:
        target: Результат writer_scope с credentials и конечными limits.

    Returns:
        AsyncEngine с NullPool; caller закрывает connection и вызывает dispose.

    Raises:
        ImportError: Не установлен PostgreSQL extra.
        sqlalchemy.exc.ArgumentError: Неверная конфигурация engine.

    Соединение ещё не открывается. Instance loggers подавлены, parameters
    скрыты; глобальная logging configuration не меняется. Проверка реальных
    grants принадлежит writer transaction, не созданию engine.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    limits = target.limits
    engine = create_async_engine(
        make_url(target.dsn.get_secret_value()),
        poolclass=NullPool,
        isolation_level="READ COMMITTED",
        echo=False,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "default_transaction_read_only": "off",
                "default_transaction_isolation": "read committed",
                "statement_timeout": str(
                    max(1, int(limits.statement_timeout_seconds * 1000))
                ),
                "lock_timeout": str(max(1, int(limits.lock_timeout_seconds * 1000))),
                "search_path": "pg_catalog",
                "application_name": "structuraguard-loader",
            },
            "timeout": min(limits.timeout_seconds, limits.statement_timeout_seconds),
            "command_timeout": limits.statement_timeout_seconds
            + limits.cleanup_seconds,
            "prepared_statement_cache_size": 0,
        },
    )
    quiet = logging.Logger("structuraguard.loader.private", logging.CRITICAL + 1)
    quiet.propagate = False
    engine.sync_engine.logger = quiet
    engine.sync_engine.pool.logger = quiet
    return engine
