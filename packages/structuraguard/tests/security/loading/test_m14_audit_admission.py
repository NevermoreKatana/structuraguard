"""D4: отсутствие обязательного audit отказывает до первого DB connection."""

from uuid import UUID

import pytest
from pydantic import SecretStr
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case
from tests.fakes.staging import StagingClock

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.database_policy import (
    DatabasePolicy,
    DatabaseTableRule,
    PostgreSQLAuditPolicy,
)
from structuraguard.contracts.loading import PostgreSQLLoadPolicy
from structuraguard.contracts.staging import StagingRetentionPolicy
from structuraguard.database import PostgreSQLTarget
from structuraguard.database.loader import PostgreSQLLoader
from structuraguard.database.writer_target import PostgreSQLWriterTarget
from structuraguard.exceptions import LoadError
from structuraguard.security.database import bind_database_policy
from structuraguard.stores import MemoryStagingStore


@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["policy", "signer"])
async def test_central_policy_requires_signed_audit_before_io(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlalchemy.ext.asyncio

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Отсутствующий audit не должен открывать DB engine")

    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", forbidden)
    _, _, catalog, _, _ = await case()
    _, preflight = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="orders",
    )
    inspector = bind_database_policy(
        PostgreSQLTarget(
            dsn=SecretStr("postgresql+asyncpg://inspector:fixture@localhost/db"),
            target_id=catalog.target_id,
            include_schemas=("public",),
            include_tables=(("public", "orders"),),
        ),
        DatabasePolicy(
            allowed_schemas=("public",),
            allowed_tables=(
                DatabaseTableRule(
                    schema_name="public", table_name="orders", columns=("id", "amount")
                ),
            ),
            inspector_principal="inspector",
            writer_principal="dry_writer",
        ),
    )
    target = PostgreSQLWriterTarget(
        inspection=inspector,
        dsn=SecretStr("postgresql+asyncpg://dry_writer:fixture@localhost/db"),
        principal="dry_writer",
    )
    policy = PostgreSQLLoadPolicy(
        preflight=preflight,
        write_tables=(("public", "orders"),),
        audit=PostgreSQLAuditPolicy(
            schema_name="sg_staging_audit_fixture",
            namespace=UUID(int=1),
            actor_id=UUID(int=2),
            key_id=UUID(int=3),
        )
        if missing == "signer"
        else None,
    )
    clock = StagingClock()
    store = MemoryStagingStore(
        target_id=catalog.target_id, retention=StagingRetentionPolicy(), clock=clock
    )
    with pytest.raises(LoadError, match="AUDIT_DURABILITY_REQUIRED"):
        PostgreSQLLoader(target, policy=policy, staging=store, clock=clock)
