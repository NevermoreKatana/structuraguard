"""M14: общий store contract, persistence и CAS на разных PostgreSQL connections."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
from tests.contract_suites.audit import (
    assert_audit_store_contract,
    audit_chain,
    audit_event,
)

from structuraguard.contracts.database_policy import PostgreSQLAuditPolicy
from structuraguard.database.audit import (
    PostgreSQLAuditChainStore,
    audit_layout_fingerprint,
    audit_tables,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security.audit_store import MemoryAuditChainStore

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.database_integration,
]


@dataclass(frozen=True)
class AuditDatabase:
    writer: AsyncEngine
    admin: AsyncEngine
    policy: PostgreSQLAuditPolicy


@pytest.fixture
async def audit_database(pg_dsn: str) -> AsyncIterator[AuditDatabase]:
    schema = "sg_staging_audit_contract"
    policy = PostgreSQLAuditPolicy(
        schema_name=schema,
        namespace=UUID(int=10),
        actor_id=UUID(int=3),
        key_id=UUID(int=4),
    )
    admin = create_async_engine(pg_dsn, poolclass=NullPool)
    writer_dsn = make_url(pg_dsn).set(
        username="dry_writer", password="dry-writer-canary-77"
    )
    writer = create_async_engine(writer_dsn, poolclass=NullPool)
    try:
        async with admin.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            metadata, tables = audit_tables(schema)
            await connection.run_sync(metadata.create_all)
            await connection.execute(
                insert(tables["schema_info"]),
                {"version": 1, "fingerprint": audit_layout_fingerprint(schema)},
            )
            await connection.exec_driver_sql(
                f'GRANT USAGE ON SCHEMA "{schema}" TO dry_writer'
            )
            await connection.exec_driver_sql(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO dry_writer'
            )
            await connection.exec_driver_sql(
                f'GRANT INSERT ON "{schema}".chain_events, "{schema}".delivery_intents TO dry_writer'
            )
        yield AuditDatabase(writer, admin, policy)
    finally:
        await writer.dispose()
        async with admin.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
        await admin.dispose()


async def test_postgresql_audit_store_contract(audit_database: AuditDatabase) -> None:
    async with audit_database.writer.begin() as connection:
        store = PostgreSQLAuditChainStore(connection, audit_database.policy)
        await store.prepare()
        last = await assert_audit_store_contract(store)
    # Новый connection/store после COMMIT не зависит от предыдущего process state.
    async with audit_database.writer.begin() as connection:
        restarted = PostgreSQLAuditChainStore(connection, audit_database.policy)
        await restarted.prepare()
        assert (await audit_chain(restarted).verify(expected_head=last.head)).count == 2
        tables = audit_tables(audit_database.policy.schema_name)[1]
        intents = (await connection.execute(select(tables["delivery_intents"]))).all()
        assert len(intents) == 2


async def test_prepared_store_cannot_cross_transaction_boundary(
    audit_database: AuditDatabase,
) -> None:
    async with audit_database.writer.connect() as connection:
        store = PostgreSQLAuditChainStore(connection, audit_database.policy)
        with pytest.raises(SecurityPolicyError, match="AUDIT_DURABILITY_REQUIRED"):
            await store.prepare()
        async with connection.begin():
            await store.prepare()
            first = await audit_chain(store).append(audit_event())
        async with connection.begin():
            with pytest.raises(SecurityPolicyError, match="AUDIT_DURABILITY_REQUIRED"):
                await store.tail(first.chain_id)
            await store.prepare()
            assert await store.tail(first.chain_id) == first


async def test_rollback_removes_both_event_and_intent_after_reconnect(
    audit_database: AuditDatabase,
) -> None:
    async with audit_database.writer.connect() as connection:
        transaction = await connection.begin()
        store = PostgreSQLAuditChainStore(connection, audit_database.policy)
        await store.prepare()
        first = await audit_chain(store).append(audit_event())
        await transaction.rollback()
    async with audit_database.writer.begin() as connection:
        store = PostgreSQLAuditChainStore(connection, audit_database.policy)
        await store.prepare()
        assert await store.tail(first.chain_id) is None
        for table in audit_tables(audit_database.policy.schema_name)[1].values():
            if table.name != "schema_info":
                assert (await connection.execute(select(table))).all() == []


async def test_concurrent_connections_cannot_fork_chain_or_duplicate_intent(
    audit_database: AuditDatabase,
) -> None:
    envelope = await audit_chain(MemoryAuditChainStore()).append(audit_event())
    appended = asyncio.Event()
    contender_ready = asyncio.Event()

    async def first() -> bool:
        async with audit_database.writer.begin() as connection:
            store = PostgreSQLAuditChainStore(connection, audit_database.policy)
            await store.prepare()
            result = await store.compare_append(envelope, None)
            appended.set()
            await contender_ready.wait()
            return result

    async def contender() -> bool:
        async with audit_database.writer.begin() as connection:
            store = PostgreSQLAuditChainStore(connection, audit_database.policy)
            await store.prepare()
            await appended.wait()
            contender_ready.set()
            return await store.compare_append(envelope, None)

    async with asyncio.timeout(10):
        accepted, rejected = await asyncio.gather(first(), contender())
        assert accepted is True and rejected is False
    async with audit_database.writer.begin() as connection:
        for name in ("chain_events", "delivery_intents"):
            table = audit_tables(audit_database.policy.schema_name)[1][name]
            assert len((await connection.execute(select(table))).all()) == 1
