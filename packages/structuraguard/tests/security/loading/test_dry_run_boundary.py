"""Невалидный snapshot не открывает БД; значения не появляются в DTO результата."""

import pytest
from pydantic import SecretStr
from tests.fakes.mapping_validation import case
from tests.unit.loading.test_projection import request

from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
from structuraguard.contracts.loading import DryRunPolicy, DryRunRequest
from structuraguard.database import PostgreSQLTarget
from structuraguard.database.dry_run import PostgreSQLDryRunPlanner
from structuraguard.database.postgresql import PostgreSQLDatabaseAdapter
from structuraguard.exceptions import LoadError
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


async def test_forged_payload_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Невалидный input открыл engine")

    monkeypatch.setattr(PostgreSQLDatabaseAdapter, "_engine", forbidden)
    _, _, _, mapping_policy, _ = await case()
    target = PostgreSQLTarget(
        dsn=SecretStr("postgresql+asyncpg://inspector:secret-canary@localhost/db"),
        target_id="main",
        include_schemas=("public",),
        include_tables=(("public", "orders"),),
    )
    policy = DryRunPolicy(
        writer_principal="writer",
        mapping_policy=mapping_policy,
        read_policy=ConstraintReadPolicy(allow_columns=()),
    )
    incoming = (await request()).model_copy(update={"batches": ()})
    with pytest.raises(LoadError, match="DRY_RUN_INPUT_INVALID"):
        await PostgreSQLDryRunPlanner(target, policy=policy).plan(incoming)
    assert "secret-canary" not in repr(target)


async def test_input_budget_precedes_dump_and_hash() -> None:
    incoming = await request()
    forged = incoming.mapping.model_copy(update={"plan_id": "x" * 65537})
    with pytest.raises(LoadError, match="SECURITY_LIMIT_EXCEEDED"):
        await prepare(
            DryRunRequest.model_construct(batches=incoming.batches, mapping=forged)
        )


async def test_changed_normalized_content_cannot_reuse_manifest() -> None:
    incoming = await request()
    record = incoming.batches[0].records[0]
    altered = record.model_copy(update={"record_id": "changed"})
    batch = incoming.batches[0].model_copy(update={"records": (altered,)})
    with pytest.raises(LoadError, match="DRY_RUN_INPUT_INVALID"):
        await prepare(
            incoming.model_copy(update={"batches": (batch, *incoming.batches[1:])})
        )
