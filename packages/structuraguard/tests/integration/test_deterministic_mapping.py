"""Реальный SQLite inspector + M8 profiler → bounded deterministic candidates."""

import sqlite3
from pathlib import Path

import pytest
from tests.fakes.mapping import profile, refs

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.deterministic_mapping import MappingScope
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.mapping import DeterministicMapper

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_real_catalog_and_profile_are_consumed_without_loading(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mapping.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT, generated TEXT GENERATED ALWAYS AS (email) STORED)"
        )
    target = SQLiteTarget(path=path, target_id="mapping", include_tables=("customers",))
    db = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    result = await DeterministicMapper().rank(
        await profile(),
        db,
        scope=MappingScope(
            target_id=db.target_id,
            target_policy_fingerprint=db.target_policy_fingerprint,
            allow=refs(db),
        ),
    )
    candidate = result.fields[0].candidates[0]
    assert any(
        c.column_id == candidate.target.column_id and c.name == "email"
        for s in db.schemas
        for t in s.tables
        for c in t.columns
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM customers").fetchone() == (0,)
