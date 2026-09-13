"""Явный административный bootstrap append-only ledger в target PostgreSQL DB."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts.loading import LoadLedgerPolicy
from structuraguard.contracts.staging import StagingLimits
from structuraguard.loading.projection import failure
from structuraguard.stores._core import checked
from structuraguard.stores.postgresql import PostgreSQLStagingTarget, _session, _target

from ._load_ledger_schema import check_schema, layout_fingerprint, tables

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


async def bootstrap_postgresql_loader(
    target: PostgreSQLStagingTarget, *, limits: StagingLimits | None = None
) -> None:
    """Явно создать выделенную ledger schema отдельной административной ролью.

    Args:
        target: Конфигурация с purpose='bootstrap', admin DSN той же target DB
            и schema sg_staging_load_*, согласованной с LoadLedgerPolicy.
        limits: Конечные statement/transaction/cleanup budgets; None — defaults.

    Returns:
        None после создания либо проверки существующей совместимой схемы.

    Raises:
        LoadError: LOAD_LEDGER_BOOTSTRAP_REQUIRED для runtime target либо отказ
            ledger validation. StagingError также совместим с LoadError.
        StagingError: Неверный target, privileges, timeout, DB/cleanup failure
            или неизвестный исход COMMIT; driver details не раскрываются.
        pydantic.ValidationError: Некорректные поля LoadLedgerPolicy.
        asyncio.CancelledError: Отмена после cleanup.

    Создаёт только выделенную schema и четыре таблицы ledger, записывает версию.
    GRANT, DROP и миграции не выполняются. Credentials не передаются loader;
    import/ingest никогда не вызывают bootstrap автоматически.
    """
    target = _target(target)
    if target.purpose != "bootstrap":
        raise failure("LOAD_LEDGER_BOOTSTRAP_REQUIRED")
    policy = LoadLedgerPolicy(
        namespace=target.namespace, schema_name=target.schema_name
    )
    limits = checked(limits or StagingLimits(), StagingLimits, StagingLimits())

    async def create(connection: AsyncConnection) -> None:
        from sqlalchemy import insert, text
        from sqlalchemy.schema import CreateSchema

        await connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:scope,0))"
            ),
            {"scope": f"structuraguard-loader-bootstrap:{policy.schema_name}"},
        )
        exists = (
            await connection.execute(
                text(
                    "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=:schema)"
                ),
                {"schema": policy.schema_name},
            )
        ).scalar_one()
        if not exists:
            metadata, mapping = tables(policy.schema_name)
            await connection.execute(CreateSchema(policy.schema_name))
            await connection.run_sync(
                lambda sync: metadata.create_all(sync, checkfirst=False)
            )
            await connection.execute(
                insert(mapping["schema_info"]),
                {
                    "version": 1,
                    "fingerprint": layout_fingerprint(policy.schema_name),
                },
            )
        await check_schema(connection, policy.schema_name)

    await _session(target, limits, create)
