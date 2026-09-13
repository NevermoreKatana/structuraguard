"""M15: source→report на PostgreSQL 16/18 через существующие adapters."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.fakes.mapping import scope_for
from tests.fakes.pipeline import defaults, engine, request
from tests.integration.database.test_postgresql_dry_run import Database
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_load_outcomes import LedgerDatabase
from tests.integration.database.test_postgresql_load_outcomes import (
    ledger_db as ledger_db,
)

from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts import TransactionOutcome
from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.loading import (
    DryRunPolicy,
    DryRunRequest,
    LoadLedgerPolicy,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.reports import AuditEvent
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.contracts.staging import (
    StagingArtifactKind,
    StagingArtifactReference,
    StagingRetentionPolicy,
)
from structuraguard.database import PostgreSQLDatabaseAdapter, _load_transaction
from structuraguard.database.constraint_reader import DatabaseConstraintReader
from structuraguard.database.dry_run import PostgreSQLDryRunPlanner
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.exceptions import LoadError, StructuraGuardError
from structuraguard.pipeline import DatabaseBinding
from structuraguard.pipeline.composition import Loader
from structuraguard.security.session import SecuritySession
from structuraguard.stores import MemoryStagingStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


@pytest.fixture
async def sdk_db(dry_db: Database) -> AsyncIterator[Database]:
    db = dry_db
    await db.sql("CREATE TABLE $schema.records (field_0 text NOT NULL, field_1 text)")
    await db.sql(
        'CREATE UNIQUE INDEX records_identity ON $schema.records (field_0 COLLATE "C")'
    )
    await db.sql("GRANT SELECT ON $schema.records TO inspector")
    await db.sql("GRANT SELECT, INSERT, UPDATE ON $schema.records TO dry_writer")
    db.target = db.target.model_copy(
        update={"include_tables": ((db.schema, "records"),)}
    )
    db.catalog = await PostgreSQLDatabaseAdapter(db.target).inspect(
        DatabaseInspectionRequest(
            target_id=db.target.target_id,
            target_policy_fingerprint=db.target.policy_fingerprint,
        )
    )
    yield db


def composition(
    db: Database,
    pg_dsn: str,
    *,
    ledger: LoadLedgerPolicy | None = None,
) -> Callable[[SecuritySession], DatabaseBinding]:
    scope = scope_for(db.catalog)
    policy = DryRunPolicy(
        writer_principal="dry_writer",
        mapping_policy=MappingValidationPolicy(
            policy_id="sdk",
            scope=scope,
            allow_schemas=(db.schema,),
            allow_tables=((db.schema, "records"),),
            source_identity_allow=scope.allow,
        ),
        read_policy=ConstraintReadPolicy(allow_columns=scope.allow),
    )
    writer = PostgreSQLWriterTarget(
        inspection=db.target,
        principal="dry_writer",
        dsn=SecretStr(
            make_url(pg_dsn)
            .set(username="dry_writer", password="dry-writer-canary-77")
            .render_as_string(hide_password=False)
        ),
    )
    store = MemoryStagingStore(
        target_id=db.target.target_id, retention=StagingRetentionPolicy()
    )
    retained: list[DryRunRequest] = []

    async def references(
        snapshot: DryRunRequest, run_id: str, deadline: datetime
    ) -> tuple[StagingArtifactReference, ...]:
        retained.append(snapshot)
        plan = snapshot.mapping
        return tuple(
            StagingArtifactReference(
                kind=kind,
                artifact_id=f"fixture-{kind}-{run_id}",
                fingerprint=plan.normalized_fingerprint
                if kind is StagingArtifactKind.NORMALIZED
                else plan.fingerprint
                if kind is StagingArtifactKind.MAPPING
                else plan.source_fingerprint,
                retained_until=deadline + timedelta(days=90),
            )
            for kind in StagingArtifactKind
        )

    def bind(resources: SecuritySession) -> DatabaseBinding:
        return DatabaseBinding(
            inspector=PostgreSQLDatabaseAdapter(db.target, resources=resources),
            request=DatabaseInspectionRequest(
                target_id=db.target.target_id,
                target_policy_fingerprint=db.target.policy_fingerprint,
            ),
            policy=policy,
            reader=DatabaseConstraintReader(
                db.target, policy=policy.read_policy, resources=resources
            ),
            planner=PostgreSQLDryRunPlanner(
                db.target, policy=policy, resources=resources
            ),
            loader=PostgreSQLLoader(
                writer,
                policy=PostgreSQLLoadPolicy(
                    preflight=policy,
                    write_tables=((db.schema, "records"),),
                    batch_size=1,
                    ledger=ledger,
                ),
                staging=store,
                resources=resources,
            ),
            staging=store,
            references=references,
        )

    return bind


async def rows(db: Database) -> tuple[tuple[object, ...], ...]:
    async with db.engine.connect() as conn:
        return tuple(
            tuple(row)
            for row in (
                await conn.exec_driver_sql(
                    f'SELECT * FROM "{db.schema}".records ORDER BY 1'
                )
            ).all()
        )


@pytest.mark.parametrize("dry_run", (True, False))
async def test_source_to_report(sdk_db: Database, pg_dsn: str, dry_run: bool) -> None:
    db = sdk_db
    deps = replace(defaults(), database=composition(db, pg_dsn))
    before = await db.snapshot()
    result = await engine(dependencies=deps).ingest(request(), dry_run=dry_run)
    assert result.status is S.COMPLETED, result.errors
    assert result.validation_report and result.validation_report.complete
    assert result.load_report and result.load_report.loaded_records == (
        0 if dry_run else 2
    )
    assert await rows(db) == (() if dry_run else (("Ada", "Riga"), ("Bob", "Oslo")))
    assert await db.snapshot() == before


@pytest.mark.parametrize("change", ("schema", "grants"))
async def test_drift_and_permission_errors_prevent_load(
    sdk_db: Database, pg_dsn: str, change: str
) -> None:
    db = sdk_db

    async def hook(event: AuditEvent) -> None:
        if event.status is S.STAGING and event.event_type == "stage_started":
            await db.sql(
                "ALTER TABLE $schema.records ADD COLUMN changed text"
                if change == "schema"
                else "REVOKE INSERT ON $schema.records FROM dry_writer"
            )

    result = await engine(
        dependencies=replace(
            defaults(), database=composition(db, pg_dsn), hooks=(hook,)
        )
    ).ingest(request())
    assert result.status in {S.ROLLED_BACK, S.NEEDS_REVIEW, S.FAILED}
    assert (
        result.errors and result.transaction_outcome is not TransactionOutcome.COMMITTED
    )
    assert await rows(db) == ()


def fail_after_write[**P, T](
    original: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        await original(*args, **kwargs)
        raise RuntimeError("synthetic-after-real-DML")

    return wrapper


async def test_failure_after_actual_dml_rolls_back(
    sdk_db: Database, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = sdk_db
    monkeypatch.setattr(
        _load_transaction, "_write", fail_after_write(_load_transaction._write)
    )
    result = await engine(
        dependencies=replace(defaults(), database=composition(db, pg_dsn))
    ).ingest(request())
    assert result.status is S.ROLLED_BACK, result.errors
    assert result.transaction_outcome is TransactionOutcome.ROLLED_BACK
    assert await rows(db) == ()


class CapturedLoader:
    def __init__(self, delegate: Loader) -> None:
        self.delegate = delegate
        self.requests: list[LoadRequest] = []

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        self.requests.append(request)
        return await self.delegate.execute(request)


async def test_rejected_concurrent_execute_preserves_dry_run(
    sdk_db: Database, pg_dsn: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def hook(event: AuditEvent) -> None:
        if event.status is S.STAGING and event.event_type == "stage_started":
            entered.set()
            await release.wait()

    sdk = engine(
        dependencies=replace(
            defaults(), database=composition(sdk_db, pg_dsn), hooks=(hook,)
        )
    )
    source = await sdk.inspect_source(request())
    before = await sdk_db.snapshot()
    async with source:
        parse = await sdk.create_parse_plan(source)
        assert parse
        data = await sdk.parse_semantically(source, plan=parse)
        mapping = await sdk.create_mapping_plan(data)
        assert mapping.plan
        task = asyncio.create_task(sdk.execute(data, plan=mapping.plan, dry_run=True))
        try:
            async with asyncio.timeout(5):
                await entered.wait()
                with pytest.raises(StructuraGuardError, match="SDK_RUN_BUSY"):
                    await sdk.execute(data, plan=mapping.plan, dry_run=False)
        finally:
            release.set()
            result = await task
    assert await rows(sdk_db) == ()
    assert await sdk_db.snapshot() == before
    assert result.status is S.COMPLETED and result.dry_run
    assert result.transaction_outcome is TransactionOutcome.DRY_RUN


class LostResponseLoader(CapturedLoader):
    def __init__(self, delegate: Loader, failure: str) -> None:
        super().__init__(delegate)
        self.failure = failure

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        await super().execute(request)
        if self.failure == "cancel":
            raise asyncio.CancelledError
        raise LoadError(error_code="LOAD_CONNECTION_FAILED", message="lost reply")


@pytest.mark.parametrize("failure", ("error", "cancel"))
async def test_committed_staging_preserves_outcome_after_lost_response(
    sdk_db: Database, pg_dsn: str, failure: str
) -> None:
    factory = composition(sdk_db, pg_dsn)
    loaders: list[LostResponseLoader] = []

    def bind(resources: SecuritySession) -> DatabaseBinding:
        binding = factory(resources)
        assert binding.loader
        loader = LostResponseLoader(binding.loader, failure)
        loaders.append(loader)
        return replace(binding, loader=loader)

    result = await engine(dependencies=replace(defaults(), database=bind)).ingest(
        request()
    )
    assert await rows(sdk_db) == (("Ada", "Riga"), ("Bob", "Oslo"))
    assert result.status is S.COMPLETED_WITH_WARNINGS
    assert result.transaction_outcome is TransactionOutcome.COMMITTED
    assert (
        "LOAD_CANCELLED_AFTER_COMMIT"
        if failure == "cancel"
        else "LOAD_CONNECTION_FAILED"
    ) in result.warnings
    assert result.load_report is None and result.load_result is None
    assert len(loaders) == 1 and len(loaders[0].requests) == 1


async def test_sdk_unknown_commit_can_be_reconciled_by_same_ledger_binding(
    sdk_db: Database,
    ledger_db: LedgerDatabase,
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = sdk_db
    factory = composition(db, pg_dsn, ledger=ledger_db.policy)
    adapters: list[CapturedLoader] = []
    policies: list[SecurityPolicy] = []

    def capture(resources: SecuritySession) -> DatabaseBinding:
        binding = factory(resources)
        assert binding.loader
        adapter = CapturedLoader(binding.loader)
        adapters.append(adapter)
        policies.append(resources.policy)
        return replace(binding, loader=adapter)

    original = AsyncConnection.commit

    async def lose_response(self: AsyncConnection) -> None:
        await original(self)
        raise OSError("commit-response-secret-canary")

    # Ошибка после действительного COMMIT моделирует потерю ответа сервера.
    monkeypatch.setattr(AsyncConnection, "commit", lose_response)
    result = await engine(dependencies=replace(defaults(), database=capture)).ingest(
        request(),
        idempotency_key="sdk-private-idempotency-key",
    )
    monkeypatch.setattr(AsyncConnection, "commit", original)
    assert result.status is S.FAILED
    assert result.transaction_outcome is TransactionOutcome.UNKNOWN
    assert result.errors[-1].code == "LOAD_OUTCOME_UNKNOWN"
    assert len(adapters) == 1 and len(adapters[0].requests) == 1
    original_request = adapters[0].requests[0]
    assert original_request.idempotency_key == "sdk-private-idempotency-key"
    assert await rows(db) == (("Ada", "Riga"), ("Bob", "Oslo"))
    before = await ledger_db.unchanged()
    before_target = await rows(db)
    # Ошибка закрыла старый guard: reconciliation — явная операция host
    # с новым бюджетом и прежним sealed request, а не скрытый SDK retry.
    recovery = factory(
        SecuritySession(
            policies[0],
            run_id=UUID("aaaaaaaa-bbbb-4ccc-addd-eeeeeeeeeeee"),
        )
    )
    assert recovery.loader
    recovered = await recovery.loader.execute(original_request)
    assert recovered.replayed and recovered.run_id == result.run_id
    assert recovered.safe_summary()["inserted"] == 0
    assert await rows(db) == before_target
    assert await ledger_db.unchanged() == before
    assert len(await ledger_db.rows("execution_commits")) == 1
    assert "sdk-private-idempotency-key" not in repr(
        await ledger_db.rows("execution_audit")
    )
    assert "commit-response-secret-canary" not in result.model_dump_json()
