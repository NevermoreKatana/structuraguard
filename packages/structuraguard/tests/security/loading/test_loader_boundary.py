"""Неверная staging binding и writer identity не открывают writer connection."""

import pytest
from pydantic import SecretStr
from tests.fakes.loading import dry_run_case, sealed_input
from tests.fakes.mapping_validation import case, replan
from tests.fakes.staging import StagingClock

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.database import PostgreSQLTarget
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.exceptions import LoadError
from structuraguard.stores import MemoryStagingStore

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("kind", ["mapping", "revision", "endpoint", "principal"])
async def test_rejects_unbound_input_before_writer_io(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.database import _load_transaction

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Неверный input открыл writer")

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
    request = await sealed_input(snapshot, store, clock)
    if kind == "mapping":
        request = request.model_copy(
            update={
                "snapshot": snapshot.model_copy(
                    update={"mapping": replan(snapshot.mapping, plan_id="other")}
                )
            }
        )
    elif kind == "revision":
        request = request.model_copy(update={"staging_revision": 999})
    inspector = PostgreSQLTarget(
        dsn=SecretStr("postgresql+asyncpg://inspector:inspection-canary@localhost/db"),
        target_id=catalog.target_id,
        include_schemas=("public",),
        include_tables=(("public", "orders"),),
    )
    endpoint = "other-db" if kind == "endpoint" else "db"
    principal = "inspector" if kind == "principal" else "dry_writer"
    target = PostgreSQLWriterTarget(
        inspection=inspector,
        dsn=SecretStr(
            f"postgresql+asyncpg://{principal}:writer-canary@localhost/{endpoint}"
        ),
        principal=principal,
    )
    policy = PostgreSQLLoadPolicy(
        preflight=preflight, write_tables=(("public", "orders"),)
    )
    with pytest.raises(LoadError) as caught:
        loader = PostgreSQLLoader(target, policy=policy, staging=store, clock=clock)
        await loader.execute(request)
    assert "writer-canary" not in str(caught.value)
    assert "inspection-canary" not in str(caught.value)
