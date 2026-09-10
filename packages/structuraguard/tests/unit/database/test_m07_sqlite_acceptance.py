"""K2/K3: отсутствующая таблица и невыполнение SQLite view expressions."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from structuraguard.contracts import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.exceptions import DatabaseInspectionError


@pytest.mark.anyio
async def test_missing_table_is_not_an_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE present(id INTEGER)")
    before = path.read_bytes()
    target = SQLiteTarget(path=path, target_id="test", include_tables=("missing",))
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id="test", target_policy_fingerprint=target.policy_fingerprint
            )
        )
    assert caught.value.error_code == "DATABASE_OBJECT_NOT_FOUND"
    assert path.read_bytes() == before
    with sqlite3.connect(path, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()


@pytest.mark.anyio
async def test_view_expression_that_would_fail_is_not_executed(tmp_path: Path) -> None:
    path = tmp_path / "view.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE VIEW dangerous AS SELECT abs(-9223372036854775808) AS boom"
        )
        with pytest.raises(sqlite3.OperationalError, match="integer overflow"):
            connection.execute("SELECT * FROM dangerous").fetchall()
    target = SQLiteTarget(path=path, target_id="test", include_tables=("dangerous",))
    catalog = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id="test", target_policy_fingerprint=target.policy_fingerprint
        )
    )
    table = catalog.schemas[0].tables[0]
    assert table.columns[0].name == "boom" and not table.writable
    assert (
        catalog.dependency_graph is not None
        and catalog.dependency_graph.load_order == ()
    )
