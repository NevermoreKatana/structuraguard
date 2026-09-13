"""Audit chain store на caller-owned PostgreSQL transaction; runtime без DDL."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING
from uuid import UUID

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.audit import AuditEnvelope, AuditHead, SecurityAuditEvent
from structuraguard.contracts.database_policy import PostgreSQLAuditPolicy
from structuraguard.ports.audit import AuditSigner
from structuraguard.security.audit import AuditChain, _safe
from structuraguard.security.audit_store import audit_failure, checked_envelope
from structuraguard.security.events import load_chain_identity

from ._load_ledger_schema import check_layout, grants

if TYPE_CHECKING:
    from sqlalchemy import ColumnElement, MetaData, Select, Table
    from sqlalchemy.ext.asyncio import AsyncConnection
    from sqlalchemy.sql.schema import SchemaItem

    from structuraguard.database.target import PostgreSQLTarget
    from structuraguard.stores.postgresql import PostgreSQLStagingTarget


def audit_tables(schema: str) -> tuple[MetaData, dict[str, Table]]:
    """Описание layout для отдельной явной admin migration, без исполнения DDL."""
    from sqlalchemy import Column, Integer, MetaData, Table, Text

    metadata = MetaData(schema=schema)
    Table(
        "schema_info",
        metadata,
        Column("version", Integer, primary_key=True, autoincrement=False),
        Column("fingerprint", Text, nullable=False),
    )
    for name in ("chain_events", "delivery_intents"):
        columns: list[SchemaItem] = [
            Column("chain_id", Text, primary_key=True),
            Column("sequence", Integer, primary_key=True, autoincrement=False),
            Column("event_id", Text, nullable=False),
        ]
        if name == "chain_events":
            columns.append(Column("payload", Text, nullable=False))
        Table(name, metadata, *columns)
    return metadata, {t.name: t for t in metadata.tables.values()}


def audit_layout_fingerprint(schema: str) -> str:
    """Вернуть hash ожидаемого layout schema без подключения и исполнения DDL."""
    return canonical_sha256_value(
        (
            "audit-chain-v1",
            schema,
            tuple(
                (
                    t.name,
                    tuple(
                        (c.name, str(c.type), c.nullable, c.primary_key)
                        for c in t.columns
                    ),
                )
                for t in sorted(audit_tables(schema)[1].values(), key=lambda t: t.name)
            ),
        )
    )


class PostgreSQLAuditChainStore:
    """Добавлять audit event и delivery intent в ту же transaction, что target DML.

    connection — caller-owned AsyncConnection с открытой transaction;
    policy — PostgreSQLAuditPolicy заранее установленной выделенной schema.
    Конструктор валидирует DTO без I/O. Prepare проверяет layout/grants; остальные
    методы требуют ту же transaction, иначе AUDIT_DURABILITY_REQUIRED.
    SecurityPolicyError скрывает backend diagnostics. Commit/rollback/timeout и
    закрытие connection выполняет caller; store не создаёт таблицы/новые connections.
    Writer имеет SELECT/INSERT без UPDATE/DELETE/DDL. Внешнюю доставку intent и
    acknowledgements реализует host; store не подтверждает её завершение."""

    def __init__(
        self, connection: AsyncConnection, policy: PostgreSQLAuditPolicy
    ) -> None:
        self._connection = connection
        self._policy = PostgreSQLAuditPolicy.model_validate(policy.model_dump())
        self._tables = audit_tables(self._policy.schema_name)[1]
        self._ready = False

    async def prepare(self) -> None:
        """Проверить layout/grants до DML; разрешение живёт только в transaction."""
        await _safe(self._prepare)

    async def _prepare(self) -> None:
        if not self._connection.in_transaction():
            raise audit_failure("AUDIT_DURABILITY_REQUIRED")
        await check_layout(
            self._connection,
            self._policy.schema_name,
            self._tables,
            audit_layout_fingerprint(self._policy.schema_name),
        )
        await grants(
            self._connection, self._policy.schema_name, names=tuple(self._tables)
        )
        self._transaction = self._connection.get_transaction()
        self._ready = True

    def _check(self) -> None:
        if (
            not self._ready
            or not self._connection.in_transaction()
            or self._connection.get_transaction() is not self._transaction
        ):
            raise audit_failure("AUDIT_DURABILITY_REQUIRED")

    async def read(
        self, chain_id: UUID, *, after: int, limit: int
    ) -> tuple[AuditEnvelope, ...]:
        """Прочитать bounded ordered page chain_id после sequence=after.

        Возвращает tuple максимум limit envelopes, без подтверждения их HMAC.
        Нужна prepare в текущей transaction; invalid page/payload даёт SecurityPolicyError."""
        self._check()
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 256
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        from sqlalchemy import func, select

        table = self._tables["chain_events"]
        return await self._rows(
            select(table.c.sequence, func.left(table.c.event_id, 37), self._bounded())
            .where(table.c.chain_id == str(chain_id), table.c.sequence > after)
            .order_by(table.c.sequence)
            .limit(limit)
        )

    def _bounded(self) -> ColumnElement[str | None]:
        from sqlalchemy import case, func

        column = self._tables["chain_events"].c.payload
        return case((func.octet_length(column) <= 16_384, column), else_=None)

    async def _rows(
        self, statement: Select[tuple[int, str, str | None]]
    ) -> tuple[AuditEnvelope, ...]:
        return await _safe(partial(self._read_rows, statement))

    async def _read_rows(
        self, statement: Select[tuple[int, str, str | None]]
    ) -> tuple[AuditEnvelope, ...]:
        rows = (await self._connection.execute(statement)).all()
        output = []
        for sequence, event_id, row in rows:
            if not isinstance(row, str) or len(row.encode()) > 16_384:
                raise audit_failure("AUDIT_EVENT_INVALID")
            envelope = checked_envelope(AuditEnvelope.model_validate_json(row))
            if (
                envelope.canonical_json() != row
                or envelope.sequence != sequence
                or str(envelope.event.event_id) != event_id
            ):
                raise audit_failure("AUDIT_CHAIN_INVALID")
            output.append(envelope)
        return tuple(output)

    async def tail(self, chain_id: UUID) -> AuditEnvelope | None:
        """Прочитать последний envelope chain_id или None в подготовленной transaction."""
        self._check()
        from sqlalchemy import func, select

        table = self._tables["chain_events"]
        rows = await self._rows(
            select(table.c.sequence, func.left(table.c.event_id, 37), self._bounded())
            .where(table.c.chain_id == str(chain_id))
            .order_by(table.c.sequence.desc())
            .limit(1)
        )
        return rows[0] if rows else None

    async def find(self, chain_id: UUID, event_id: UUID) -> AuditEnvelope | None:
        """Найти envelope по chain_id/event_id; вернуть None при отсутствии, без записи."""
        self._check()
        from sqlalchemy import func, select

        table = self._tables["chain_events"]
        rows = await self._rows(
            select(table.c.sequence, func.left(table.c.event_id, 37), self._bounded())
            .where(table.c.chain_id == str(chain_id), table.c.event_id == str(event_id))
            .limit(2)
        )
        if len(rows) > 1:
            raise audit_failure("AUDIT_CHAIN_INVALID")
        return rows[0] if rows else None

    async def compare_append(
        self, envelope: AuditEnvelope, expected: AuditHead | None
    ) -> bool:
        """Вернуть результат atomic CAS expected head с event+intent INSERT.

        False означает конфликт; неверный envelope/transaction даёт SecurityPolicyError.
        Обе записи участвуют в caller transaction; метод не выполняет COMMIT.
        HMAC проверяет AuditChain, store обеспечивает persistence и concurrency contract."""
        return await _safe(partial(self._compare_append, envelope, expected))

    async def _compare_append(
        self, envelope: AuditEnvelope, expected: AuditHead | None
    ) -> bool:
        self._check()
        from sqlalchemy import insert, text

        envelope = checked_envelope(envelope)
        if envelope.sequence > self._policy.max_events:
            raise audit_failure("AUDIT_LIMIT_EXCEEDED")
        await self._connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:scope,0))"
            ),
            {"scope": f"audit-v1:{self._policy.schema_name}:{envelope.chain_id}"},
        )
        tail = await self.tail(envelope.chain_id)
        if (tail.head if tail else None) != expected or await self.find(
            envelope.chain_id, envelope.event.event_id
        ) is not None:
            return False
        if envelope.sequence != (
            tail.sequence + 1 if tail else 1
        ) or envelope.previous_hash != (tail.current_hash if tail else "0" * 64):
            raise audit_failure("AUDIT_CHAIN_INVALID")
        identity = {
            "chain_id": str(envelope.chain_id),
            "sequence": envelope.sequence,
            "event_id": str(envelope.event.event_id),
        }
        await self._connection.execute(
            insert(self._tables["chain_events"]),
            {**identity, "payload": envelope.canonical_json()},
        )
        await self._connection.execute(
            insert(self._tables["delivery_intents"]), identity
        )
        return True


async def bootstrap_postgresql_audit(
    target: PostgreSQLStagingTarget, policy: PostgreSQLAuditPolicy
) -> None:
    """Явная admin migration; runtime loader её никогда не вызывает.

    Требует purpose=bootstrap, отдельный admin target и совпадающую schema.
    Создаёт append-only tables/version. Grants выполняет deployment owner;
    автоматического ALTER/DROP либо upgrade старого ledger нет.

    Args:
        target: PostgreSQLStagingTarget с admin credentials и purpose=bootstrap.
        policy: PostgreSQLAuditPolicy той же выделенной schema.

    Возвращает None после transaction. Уже совместимая schema только проверяется;
    несовместимая даёт typed отказ. Неверная policy — ValidationError, purpose/schema
    mismatch — SecurityPolicyError/AUDIT_BOOTSTRAP_REQUIRED; DB lifecycle ошибки
    сохраняют StagingError контракт. Метод выполняет явный DB I/O/DDL и требует
    отдельного administrative подключения, которое нельзя передавать ingest.
    """
    from sqlalchemy import insert, text
    from sqlalchemy.schema import CreateSchema

    from structuraguard.contracts.staging import StagingLimits
    from structuraguard.stores.postgresql import _session, _target

    target = _target(target)
    policy = PostgreSQLAuditPolicy.model_validate(policy.model_dump())
    if target.purpose != "bootstrap" or target.schema_name != policy.schema_name:
        raise audit_failure("AUDIT_BOOTSTRAP_REQUIRED")

    async def create(connection: AsyncConnection) -> None:
        await connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:scope,0))"
            ),
            {"scope": "audit-bootstrap:" + policy.schema_name},
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
            metadata, tables = audit_tables(policy.schema_name)
            await connection.execute(CreateSchema(policy.schema_name))
            await connection.run_sync(
                lambda conn: metadata.create_all(conn, checkfirst=False)
            )
            await connection.execute(
                insert(tables["schema_info"]),
                {
                    "version": 1,
                    "fingerprint": audit_layout_fingerprint(policy.schema_name),
                },
            )
        await check_layout(
            connection,
            policy.schema_name,
            audit_tables(policy.schema_name)[1],
            audit_layout_fingerprint(policy.schema_name),
        )

    await _session(target, StagingLimits(), create)


async def append_rollback_event(
    target: PostgreSQLTarget,
    policy: PostgreSQLAuditPolicy,
    signer: AuditSigner,
    event: SecurityAuditEvent,
    *,
    run_id: str,
) -> bool:
    """Отдельная bounded transaction после rollback; False означает видимый audit gap."""
    from .postgresql import _cleanup
    from .writer_target import writer_engine

    engine = None
    connection = None
    success = False
    try:
        async with asyncio.timeout(target.limits.cleanup_seconds):
            engine = writer_engine(target)
            connection = await engine.connect()
            await connection.begin()
            store = PostgreSQLAuditChainStore(connection, policy)
            await store.prepare()
            chain_id, audit_run_id = load_chain_identity(policy, run_id)
            chain = AuditChain(
                chain_id=chain_id,
                run_id=audit_run_id,
                key_id=policy.key_id,
                policy_fingerprint=event.policy_fingerprint,
                signer=signer,
                store=store,
                max_events=policy.max_events,
                timeout_seconds=target.limits.cleanup_seconds,
            )
            await chain.append(event)
            await connection.commit()
            success = True
    except BaseException as error:
        # Failure audit не должен скрывать уже известный rollback/cancellation.
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
    finally:
        await _cleanup(connection, engine, None, target.limits.cleanup_seconds)
    return success
