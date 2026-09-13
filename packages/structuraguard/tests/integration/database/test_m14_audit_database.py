"""Реальный PostgreSQL: central scope, transactional HMAC и immutable outbox."""

import asyncio
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import insert, select, update
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.staging import StagingClock
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_load_outcomes import LedgerDatabase
from tests.integration.database.test_postgresql_load_outcomes import (
    ledger_db as ledger_db,
)
from tests.integration.database.test_postgresql_loader import (
    contents,
    policy_for,
    writer,
)

from structuraguard.contracts.audit import AuditEnvelope
from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.database_policy import (
    DatabasePolicy,
    DatabaseTableRule,
    PostgreSQLAuditPolicy,
)
from structuraguard.contracts.staging import StagingRetentionPolicy, StagingRunStatus
from structuraguard.database import PostgreSQLDatabaseAdapter
from structuraguard.database.audit import (
    audit_layout_fingerprint,
    audit_tables,
    bootstrap_postgresql_audit,
)
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.exceptions import StructuraGuardError
from structuraguard.security.audit import AuditChain, HMACAuditSigner, MemoryAuditKeys
from structuraguard.security.audit_store import MemoryAuditChainStore
from structuraguard.security.database import bind_database_policy
from structuraguard.security.events import load_chain_identity
from structuraguard.stores import MemoryStagingStore, PostgreSQLStagingTarget

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.mark.parametrize("deny", [False, True])
async def test_postgresql_column_scope_before_full_reflection(
    dry_db: Database, deny: bool
) -> None:
    central = DatabasePolicy(
        allowed_schemas=(dry_db.schema,),
        allowed_tables=tuple(
            DatabaseTableRule(
                schema_name=t.schema_name,
                table_name=t.name,
                columns=tuple(c.name for c in t.columns),
            )
            for s in dry_db.catalog.schemas
            for t in s.tables
        ),
        denied_columns=((dry_db.schema, "parents", "amount"),) if deny else (),
        inspector_principal="inspector",
        writer_principal="dry_writer",
    )
    target = bind_database_policy(dry_db.target, central)
    request = DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )
    if deny:
        with pytest.raises(StructuraGuardError, match="DATABASE_COLUMN_NOT_ALLOWED"):
            await PostgreSQLDatabaseAdapter(target).inspect(request)
    else:
        catalog = await PostgreSQLDatabaseAdapter(target).inspect(request)
        assert catalog.schemas == dry_db.catalog.schemas
        assert (
            catalog.target_policy_fingerprint
            != dry_db.catalog.target_policy_fingerprint
        )


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "replay",
        "sign_failure",
        "cancel",
        "missing_key",
        "bad_grant",
        "outbox_failure",
        "bad_layout",
    ],
)
async def test_target_audit_outbox_share_transaction(
    dry_db: Database, pg_dsn: str, mode: str, ledger_db: LedgerDatabase
) -> None:
    schema = "sg_staging_audit_" + dry_db.schema.removeprefix("dry_")
    config = PostgreSQLAuditPolicy(
        schema_name=schema,
        namespace=UUID(int=1),
        actor_id=UUID(int=2),
        key_id=UUID(int=3),
    )
    metadata, tables = audit_tables(schema)
    async with dry_db.engine.begin() as connection:
        await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await connection.run_sync(lambda conn: metadata.create_all(conn))
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
        if mode == "bad_grant":
            await connection.exec_driver_sql(
                f'GRANT UPDATE ON "{schema}".chain_events TO dry_writer'
            )
        if mode == "bad_layout":
            await connection.exec_driver_sql(
                f'ALTER TABLE "{schema}".chain_events ADD COLUMN unexpected text'
            )
    try:
        incoming, preflight = await dry_run_case(
            dry_db.catalog,
            [{"id": IntegerScalar(value=7), "amount": IntegerScalar(value=20)}],
            table_name="parents",
        )
        clock = StagingClock()
        store = MemoryStagingStore(
            target_id="main", retention=StagingRetentionPolicy(), clock=clock
        )
        request = await sealed_input(incoming, store, clock)
        policy = policy_for(dry_db, incoming, preflight).model_copy(
            update={"audit": config}
        )
        if mode == "replay":
            policy = policy.model_copy(update={"ledger": ledger_db.policy})
            request = request.model_copy(update={"idempotency_key": "m14-replay"})
        before = await contents(dry_db, "parents")
        signer = HMACAuditSigner(
            MemoryAuditKeys(
                {UUID(int=4) if mode == "missing_key" else config.key_id: b"k" * 32}
            )
        )
        entered = asyncio.Event()

        class Signer:
            async def sign(self, key_id: UUID, message: bytes) -> bytes:
                if b'"kind":"load_committed"' in message:
                    if mode == "outbox_failure":
                        async with dry_db.engine.begin() as connection:
                            await connection.exec_driver_sql(
                                f'REVOKE INSERT ON "{schema}".delivery_intents FROM dry_writer'
                            )
                    if mode == "sign_failure":
                        raise RuntimeError("restricted-raw-secret-canary")
                    if mode == "cancel":
                        entered.set()
                        await asyncio.Event().wait()
                return await signer.sign(key_id, message)

        loader = PostgreSQLLoader(
            writer(dry_db, pg_dsn),
            policy=policy,
            staging=store,
            clock=clock,
            audit_signer=Signer(),
        )
        if mode in {"success", "replay"}:
            result = await loader.execute(request)
            assert result.inserted == 1
            if mode == "replay":
                replay = await loader.execute(request)
                assert replay.replayed and replay.audit_head == result.audit_head
        elif mode == "cancel":
            task = asyncio.create_task(loader.execute(request))
            await asyncio.wait_for(entered.wait(), timeout=10)
            task.cancel("restricted-raw-secret-canary")
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert not error.value.args
        else:
            with pytest.raises(StructuraGuardError) as caught:
                await loader.execute(request)
            assert "restricted-raw-secret-canary" not in str(caught.value)
        async with dry_db.engine.connect() as connection:
            records = (
                (await connection.execute(select(tables["chain_events"].c.payload)))
                .scalars()
                .all()
            )
            intents = (
                await connection.execute(select(tables["delivery_intents"]))
            ).all()
        if mode in {"success", "replay"}:
            assert len(records) == len(intents) == 1
            envelope = AuditEnvelope.model_validate_json(records[0])
            chain_id, run_id = load_chain_identity(
                config, request.staging_context.run_id
            )
            audit = AuditChain(
                chain_id=chain_id,
                run_id=run_id,
                key_id=config.key_id,
                policy_fingerprint=envelope.event.policy_fingerprint,
                signer=signer,
                store=MemoryAuditChainStore(),
            )
            assert (
                await audit.verify_records((envelope,), expected_head=envelope.head)
            ).anchored
            assert "restricted-raw-secret-canary" not in records[0]
            if mode == "replay":
                committed_rows = await contents(dry_db, "parents")
                async with dry_db.engine.begin() as connection:
                    await connection.execute(
                        update(tables["chain_events"]).values(
                            payload=envelope.model_copy(
                                update={"current_hash": "f" * 64}
                            ).canonical_json()
                        )
                    )
                with pytest.raises(
                    StructuraGuardError, match="AUDIT_COMMITTED_UNVERIFIED"
                ):
                    await loader.execute(request)
                assert await contents(dry_db, "parents") == committed_rows
                assert (
                    await store.get_run(request.staging_context)
                ).status is StagingRunStatus.COMMITTED
        else:
            if mode in {"sign_failure", "cancel"}:
                assert len(records) == len(intents) == 1
                assert (
                    AuditEnvelope.model_validate_json(records[0]).event.kind.value
                    == "run_failed"
                )
            else:
                assert not records and not intents
                assert caught.value.details["audit_gap"] is True
            assert await contents(dry_db, "parents") == before
            assert (await store.get_run(request.staging_context)).status in {
                StagingRunStatus.SEALED,
                StagingRunStatus.ROLLED_BACK,
                StagingRunStatus.CANCELLED,
            }
    finally:
        async with dry_db.engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')


async def test_audit_bootstrap_is_explicit_and_idempotent(
    dry_db: Database, pg_dsn: str
) -> None:
    schema = "sg_staging_audit_" + dry_db.schema.removeprefix("dry_")
    policy = PostgreSQLAuditPolicy(
        schema_name=schema,
        namespace=UUID(int=1),
        actor_id=UUID(int=2),
        key_id=UUID(int=3),
    )
    target = PostgreSQLStagingTarget(
        dsn=SecretStr(pg_dsn),
        principal="test",
        inspector_principal="inspector",
        target_id="main",
        namespace="audit",
        schema_name=schema,
        purpose="bootstrap",
    )
    with pytest.raises(StructuraGuardError, match="AUDIT_BOOTSTRAP_REQUIRED"):
        await bootstrap_postgresql_audit(
            target.model_copy(update={"purpose": "writer"}), policy
        )
    try:
        await bootstrap_postgresql_audit(target, policy)
        await bootstrap_postgresql_audit(target, policy)
        async with dry_db.engine.connect() as connection:
            info = audit_tables(schema)[1]["schema_info"]
            rows = (
                await connection.execute(select(info.c.version, info.c.fingerprint))
            ).all()
            assert [tuple(row) for row in rows] == [
                (1, audit_layout_fingerprint(schema))
            ]
    finally:
        async with dry_db.engine.begin() as connection:
            await connection.exec_driver_sql(
                f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'
            )
