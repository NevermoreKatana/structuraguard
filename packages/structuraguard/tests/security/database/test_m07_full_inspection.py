"""K1/K6/K7: negative contract полного inspect до I/O обеих реализаций."""

from __future__ import annotations

from pathlib import Path
from typing import Never

import pytest
from pydantic import SecretStr
from tests.contract_suites.database import assert_request_rejection

from structuraguard.contracts import DatabaseInspectionRequest
from structuraguard.database import (
    PostgreSQLDatabaseAdapter,
    PostgreSQLTarget,
    SQLiteDatabaseAdapter,
    SQLiteTarget,
)
from structuraguard.ports.database import DatabaseAdapter


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_full_inspect_rejects_untrusted_request_before_io(
    dialect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*args: object, **kwargs: object) -> Never:
        pytest.fail("Rejected request must not reach a DB operation")

    monkeypatch.setattr(SQLiteDatabaseAdapter, "_inspect_file", unexpected_io)
    monkeypatch.setattr(PostgreSQLDatabaseAdapter, "_engine", unexpected_io)
    adapter: DatabaseAdapter
    if dialect == "sqlite":
        sqlite_target = SQLiteTarget(
            path=tmp_path / "absent.sqlite", target_id="test", include_tables=("items",)
        )
        adapter = SQLiteDatabaseAdapter(sqlite_target)
        policy = sqlite_target.policy_fingerprint
    else:
        postgres_target = PostgreSQLTarget(
            dsn=SecretStr("postgresql+asyncpg://inspector:canary@localhost/db"),
            target_id="test",
            include_schemas=("app",),
            include_tables=(("app", "items"),),
        )
        adapter = PostgreSQLDatabaseAdapter(postgres_target)
        policy = postgres_target.policy_fingerprint
    await assert_request_rejection(
        adapter,
        DatabaseInspectionRequest(target_id="test", target_policy_fingerprint=policy),
    )
