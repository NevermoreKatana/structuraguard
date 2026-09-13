"""Копируемые примеры M13 выполняются на PostgreSQL без платных внешних API."""

import importlib
import re
import sys
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, cast

import pytest
from pydantic import SecretStr
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.staging import StagingClock, staging_case
from tests.integration.database.test_postgresql_dry_run import dry_db as dry_db
from tests.integration.database.test_postgresql_load_outcomes import LedgerDatabase
from tests.integration.database.test_postgresql_load_outcomes import (
    ledger_db as ledger_db,
)
from tests.integration.database.test_postgresql_loader import (
    contents,
    policy_for,
    refresh,
    writer,
)
from tests.integration.database.test_postgresql_staging import Targets
from tests.integration.database.test_postgresql_staging import targets as targets

from structuraguard.contracts.common import IntegerScalar, LoadOperation
from structuraguard.contracts.database import ColumnCatalog
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
    LoadLedgerPolicy,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
    ServerValuePermission,
)
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.staging import (
    StagingRetentionPolicy,
    StagingRun,
    StagingRunSpec,
    StagingRunStatus,
)
from structuraguard.database import PostgreSQLTarget
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.ports import RunStagingStore
from structuraguard.stores import (
    MemoryStagingStore,
    PostgreSQLStagingStore,
    PostgreSQLStagingTarget,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.integration,
    pytest.mark.database_integration,
]


class Examples(Protocol):
    stage_dataset: Callable[
        [PostgreSQLStagingTarget, StagingRunSpec, tuple[NormalizedBatch, ...]],
        Awaitable[StagingRun],
    ]
    install_staging: Callable[[PostgreSQLStagingTarget], Awaitable[None]]
    preview_load: Callable[
        [PostgreSQLTarget, DryRunPolicy, DryRunRequest], Awaitable[DryRunExecutionPlan]
    ]
    load_staged: Callable[
        [PostgreSQLWriterTarget, PostgreSQLLoadPolicy, RunStagingStore, LoadRequest],
        Awaitable[PostgreSQLLoadResult],
    ]
    allow_server_value: Callable[[str, ColumnCatalog], ServerValuePermission]
    install_ledger: Callable[[PostgreSQLStagingTarget], Awaitable[None]]
    durable_load_policy: Callable[
        [DryRunPolicy, tuple[tuple[str, str], ...], LoadLedgerPolicy],
        PostgreSQLLoadPolicy,
    ]


@pytest.fixture
def examples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Examples]:
    root = Path(__file__).resolve().parents[5]
    modules: list[str] = []
    exports: dict[str, object] = {}
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        for name in ("staging.md", "dry_run.md", "loader.md"):
            document = (root / "docs" / name).read_text()
            blocks = re.findall(
                r"<!-- example:m13-[\w-]+:start -->\s*```python\n(.*?)\n```",
                document,
                re.DOTALL,
            )
            assert (
                len(blocks) == {"staging.md": 2, "dry_run.md": 1, "loader.md": 4}[name]
            )
            for index, code in enumerate(blocks):
                module_name = f"_m13_{Path(name).stem}_{index}"
                (tmp_path / f"{module_name}.py").write_text(code)
                assert module_name not in sys.modules
                modules.append(module_name)
                module = importlib.import_module(module_name)
                exports.update(
                    (name, value)
                    for name, value in vars(module).items()
                    if callable(value)
                    and getattr(value, "__module__", None) == module_name
                )
        yield cast(Examples, SimpleNamespace(**exports))
    finally:
        for module_name in modules:
            sys.modules.pop(module_name, None)


async def test_staging_documentation_creates_sealed_metadata(
    targets: Targets, examples: Examples
) -> None:
    await examples.install_staging(targets.admin)
    case = await staging_case(StagingClock(datetime.now(UTC)), StagingRetentionPolicy())
    run = await examples.stage_dataset(targets.writer, case.spec, case.batches)
    assert run.status is StagingRunStatus.SEALED
    assert run.record_count == case.spec.record_count
    store = PostgreSQLStagingStore(targets.writer, retention=StagingRetentionPolicy())
    assert await store.get_run(case.spec.context) == run
    records = await store.read_records(case.spec.context, offset=0, limit=100)
    assert len(records) == case.spec.record_count
    assert all(record.references == case.spec.references for record in records)


@pytest.mark.parametrize("mode", ["atomic", "quarantine_invalid"])
async def test_documented_preview_load_and_durable_replay(
    ledger_db: LedgerDatabase, pg_dsn: str, examples: Examples, mode: str
) -> None:
    db, ledger = ledger_db.db, ledger_db.policy
    admin = PostgreSQLStagingTarget(
        dsn=SecretStr(pg_dsn),
        principal="test",
        inspector_principal="inspector",
        target_id="main",
        namespace=ledger.namespace,
        schema_name=ledger.schema_name,
        purpose="bootstrap",
    )
    await examples.install_ledger(admin)
    snapshot, preflight = await dry_run_case(
        db.catalog,
        [
            {"id": IntegerScalar(value=i), "amount": IntegerScalar(value=20)}
            for i in (1, 2)
        ],
        table_name="parents",
        operation=LoadOperation.UPSERT
        if mode == "atomic"
        else LoadOperation.INSERT_ONLY,
        error_policy=mode,
    )
    clock = StagingClock(datetime.now(UTC))
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = (await sealed_input(snapshot, store, clock)).model_copy(
        update={"idempotency_key": "example-request-1"}
    )
    stage_before = (await store.get_run(request.staging_context)).canonical_json()
    before = await ledger_db.unchanged()
    plan = await examples.preview_load(db.target, preflight, snapshot)
    assert plan.ready and plan.planned_inserts == 1
    assert plan.planned_updates == (1 if mode == "atomic" else 0)
    assert plan.planned_quarantine == (0 if mode == "atomic" else 1)
    assert await ledger_db.unchanged() == before
    assert (
        await store.get_run(request.staging_context)
    ).canonical_json() == stage_before
    policy = examples.durable_load_policy(preflight, ((db.schema, "parents"),), ledger)
    target = writer(db, pg_dsn)
    result = await examples.load_staged(target, policy, store, request)
    assert not result.replayed and result.inserted == 1
    assert result.updated == plan.planned_updates
    assert result.quarantined == plan.planned_quarantine
    assert result.loaded_records + result.rejected_records == 2
    assert await contents(db, "parents") == (
        (1, 20 if mode == "atomic" else 10),
        (2, 20),
    )
    assert len(await ledger_db.rows("execution_commits")) == 1
    assert len(await ledger_db.rows("execution_audit")) == 1
    committed = await ledger_db.unchanged()
    replay = await examples.load_staged(target, policy, store, request)
    assert replay.replayed and replay.inserted == result.inserted
    assert replay.safe_summary()["inserted"] == replay.safe_summary()["updated"] == 0
    assert await ledger_db.unchanged() == committed
    assert set(plan.safe_summary()) == {
        "dry_run",
        "ready",
        "planned_inserts",
        "planned_updates",
        "planned_skips",
        "planned_quarantine",
        "blocker_count",
    }
    assert "example-request-1" not in repr(replay.safe_summary())


async def test_documented_server_permission_supports_only_explicit_load(
    ledger_db: LedgerDatabase, pg_dsn: str, examples: Examples
) -> None:
    db = ledger_db.db
    await db.sql(
        "ALTER TABLE $schema.parents ADD COLUMN doubled integer GENERATED ALWAYS AS (amount * 2) STORED"
    )
    await db.sql("GRANT SELECT (doubled) ON $schema.parents TO inspector, dry_writer")
    await refresh(db)
    snapshot, preflight = await dry_run_case(
        db.catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=21)}],
        table_name="parents",
    )
    table = next(t for s in db.catalog.schemas for t in s.tables if t.name == "parents")
    column = next(c for c in table.columns if c.name == "doubled")
    permission = examples.allow_server_value(table.table_id, column)
    policy = policy_for(db, snapshot, preflight, server_values=(permission,))
    before = await ledger_db.unchanged()
    plan = await examples.preview_load(db.target, preflight, snapshot)
    assert not plan.ready and "DRY_RUN_DEFAULT_UNVERIFIED" in plan.blockers
    assert await ledger_db.unchanged() == before
    clock = StagingClock(datetime.now(UTC))
    store = MemoryStagingStore(
        target_id="main", retention=StagingRetentionPolicy(), clock=clock
    )
    request = await sealed_input(snapshot, store, clock)
    result = await examples.load_staged(writer(db, pg_dsn), policy, store, request)
    assert result.inserted == 1
    assert await contents(db, "parents") == ((1, 10, 20), (2, 21, 42))
