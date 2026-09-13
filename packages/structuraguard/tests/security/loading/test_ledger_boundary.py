"""Ledger не позволяет stale source/plan binding, неявный bootstrap и key без policy."""

import pytest
from pydantic import SecretStr
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.mapping_validation import case
from tests.fakes.staging import StagingClock

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import LoadLedgerPolicy, PostgreSQLLoadPolicy
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.database import PostgreSQLTarget, _load_transaction
from structuraguard.database.ledger import bootstrap_postgresql_loader
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.exceptions import LoadError
from structuraguard.stores import MemoryStagingStore, PostgreSQLStagingTarget

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "bad", ["stale_snapshot", "missing_key", "missing_policy", "schema"]
)
async def test_invalid_ledger_request_does_not_open_writer(
    bad: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Неверный ledger request открыл writer")

    monkeypatch.setattr(_load_transaction, "writer_engine", forbidden)
    _, _, catalog, _, _ = await case()
    snapshot, preflight = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="orders",
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id=catalog.target_id, retention=StagingRetentionPolicy(), clock=clock
    )
    request = (await sealed_input(snapshot, store, clock)).model_copy(
        update={"idempotency_key": "secret-request-22"}
    )
    ledger = LoadLedgerPolicy(namespace="tenant")
    if bad == "stale_snapshot":
        other, _ = await dry_run_case(
            catalog,
            [{"id": IntegerScalar(value=3), "amount": IntegerScalar(value=30)}],
            table_name="orders",
        )
        request = request.model_copy(
            update={"snapshot": snapshot.model_copy(update={"batches": other.batches})}
        )
    elif bad == "missing_key":
        request = request.model_copy(update={"idempotency_key": None})
    elif bad == "schema":
        ledger = ledger.model_copy(update={"schema_name": "public; DROP TABLE users"})
    policy = PostgreSQLLoadPolicy(
        preflight=preflight, write_tables=(("public", "orders"),)
    )
    if bad != "missing_policy":
        policy = policy.model_copy(update={"ledger": ledger})
    target = PostgreSQLWriterTarget(
        inspection=PostgreSQLTarget(
            dsn=SecretStr("postgresql+asyncpg://inspector:secret-read@localhost/db"),
            target_id=catalog.target_id,
            include_schemas=("public",),
            include_tables=(("public", "orders"),),
        ),
        dsn=SecretStr("postgresql+asyncpg://dry_writer:secret-write@localhost/db"),
        principal="dry_writer",
    )
    with pytest.raises(LoadError) as caught:
        await PostgreSQLLoader(
            target, policy=policy, staging=store, clock=clock
        ).execute(request)
    assert "secret-" not in str(caught.value)
    assert "DROP TABLE" not in str(caught.value)
    assert caught.value.error_code == (
        "LOAD_INPUT_BINDING_MISMATCH"
        if bad == "stale_snapshot"
        else "DRY_RUN_INPUT_INVALID"
        if bad == "schema"
        else "LOAD_IDEMPOTENCY_REQUIRED"
    )


async def test_runtime_principal_cannot_bootstrap_ledger() -> None:
    target = PostgreSQLStagingTarget(
        dsn=SecretStr("postgresql+asyncpg://writer:secret-bootstrap@localhost/db"),
        principal="writer",
        inspector_principal="inspector",
        target_id="main",
        namespace="tenant",
        schema_name="sg_staging_load_main",
    )
    with pytest.raises(LoadError, match="LOAD_LEDGER_BOOTSTRAP_REQUIRED"):
        await bootstrap_postgresql_loader(target)
