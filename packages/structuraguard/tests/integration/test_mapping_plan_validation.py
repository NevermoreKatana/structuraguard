"""Настоящий SQLite inspection; validation не обращается к соединению или файлу."""

import sqlite3
from pathlib import Path

import pytest
from tests.fakes.mapping_validation import bind_catalog, case, replan

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.mapping import MappingPlanValidator

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.mark.parametrize("operation", [LoadOperation.INSERT_ONLY, LoadOperation.UPSERT])
async def test_sqlite_fresh_inspection_drift_and_no_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: LoadOperation
) -> None:
    path = tmp_path / "m11.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE orders(id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)"
        )
    target = SQLiteTarget(path=path, target_id="m11", include_tables=("orders",))
    adapter = SQLiteDatabaseAdapter(target)
    request = DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )
    db = await adapter.inspect(request)
    plan, manifest, _, _, profile = await case()
    plan, policy = bind_catalog(plan, db, db.schemas[0].tables[0])
    plan = replan(plan, operation=operation)
    before = path.read_bytes()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("validator attempted I/O")

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(Path, "open", forbidden)
        result = await MappingPlanValidator(policy=policy).validate(
            plan, manifest, db, profile=profile
        )
    assert result.decision is ValidationDecision.ACCEPTED
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE orders ADD COLUMN note TEXT")
    changed = await adapter.inspect(request)
    result = await MappingPlanValidator(policy=policy).validate(
        plan, manifest, changed, profile=profile
    )
    assert "DATABASE_SCHEMA_DRIFT" in {i.code for i in result.issues}
