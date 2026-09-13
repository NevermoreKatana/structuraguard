"""Явный административный bootstrap; module import не выполняет DDL."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts.staging import StagingLimits

from ._core import checked, failure
from ._postgresql_schema import check_schema, layout_fingerprint, tables
from .postgresql import PostgreSQLStagingTarget, _session, _target

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


async def bootstrap_postgresql_staging(
    target: PostgreSQLStagingTarget, *, limits: StagingLimits | None = None
) -> None:
    """Явно создать выделенную staging schema отдельной bootstrap ролью.

    Args:
        target: Trusted config с purpose='bootstrap', выделенной schema и admin DSN.
        limits: Конечные query/cleanup budgets.

    Returns:
        None после создания либо проверки совместимой существующей схемы.

    Raises:
        StagingError: Неадминистративный target, несовместимая существующая схема,
            недостающие privileges, timeout либо неизвестный исход COMMIT.
        asyncio.CancelledError: Caller cancellation после cleanup.

    Выполняет только CREATE выделенной schema/четырёх tables и INSERT версии.
    Совпадающая существующая схема проверяется без миграций. GRANT, изменение
    production schema, DROP и автоматический вызов из store/ingest отсутствуют.
    """
    target = _target(target)
    if target.purpose != "bootstrap":
        raise failure("STAGING_PRINCIPAL_FORBIDDEN")
    limits = checked(limits or StagingLimits(), StagingLimits, StagingLimits())

    async def create(connection: AsyncConnection) -> None:
        from sqlalchemy import insert, text
        from sqlalchemy.schema import CreateSchema

        # Bootstrap сериализуется только для выделенной namespace; все значения
        # bind-параметры, DDL identifiers формирует SQLAlchemy compiler.
        await connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:scope,0))"
            ),
            {"scope": f"structuraguard-bootstrap:{target.schema_name}"},
        )
        exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=:schema)"
                ),
                {"schema": target.schema_name},
            )
        ).scalar_one()
        if exists:
            await check_schema(connection, target.schema_name)
            return
        metadata, mapping = tables(target.schema_name)
        await connection.execute(CreateSchema(target.schema_name))
        await connection.run_sync(
            lambda sync: metadata.create_all(sync, checkfirst=False)
        )
        await connection.execute(
            insert(mapping["schema_info"]),
            {"version": 1, "fingerprint": layout_fingerprint(target.schema_name)},
        )

    await _session(target, limits, create)
