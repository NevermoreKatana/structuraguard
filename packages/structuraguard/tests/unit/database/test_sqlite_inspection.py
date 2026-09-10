from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.exceptions import DatabaseInspectionError


def create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE parent (
                a INTEGER NOT NULL,
                b TEXT NOT NULL,
                PRIMARY KEY (b, a),
                UNIQUE(a, b)
            ) WITHOUT ROWID;
            CREATE TABLE child (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_a INTEGER NOT NULL,
                parent_b TEXT NOT NULL,
                amount NUMERIC(12, 2) DEFAULT 1.25 CHECK (amount >= 0),
                code TEXT UNIQUE,
                doubled NUMERIC GENERATED ALWAYS AS (amount * 2) STORED,
                CHECK (length(code) > 0 OR code IS NULL),
                FOREIGN KEY (parent_b, parent_a) REFERENCES parent (b, a)
                    ON DELETE CASCADE
            );
            CREATE INDEX child_amount ON child(amount DESC, code);
            CREATE UNIQUE INDEX child_positive ON child(code) WHERE amount > 0;
            CREATE INDEX child_expression ON child(lower(code));
            CREATE TABLE secret (password TEXT);
            INSERT INTO secret VALUES ('private-value');
            """
        )


def request(target: SQLiteTarget) -> DatabaseInspectionRequest:
    return DatabaseInspectionRequest(
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
    )


@pytest.mark.anyio
async def test_sqlite_catalog_contains_real_metadata(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite"
    create_database(path)
    target = SQLiteTarget(
        path=path,
        target_id="fixture",
        include_tables=("parent", "child", "secret"),
        deny_tables=("secret",),
    )
    adapter = SQLiteDatabaseAdapter(target)
    result = await adapter.inspect_metadata(request(target))
    tables = result.schemas[0].tables
    assert [table.name for table in tables] == ["child", "parent"]
    child, parent = tables
    columns = {column.name: column for column in child.columns}
    assert list(columns) == sorted(columns)
    amount = columns["amount"]
    assert amount.nullable is True
    assert amount.inspection is not None
    assert amount.inspection.default == "1.25"
    assert amount.inspection.data_type.canonical_type == "decimal"
    assert amount.inspection.data_type.precision == 12
    assert columns["id"].nullable is False
    assert columns["id"].inspection is not None
    assert columns["id"].inspection.autoincrement is True
    generated = columns["doubled"]
    assert generated.generated and not generated.writable
    assert generated.inspection is not None
    assert generated.inspection.generation_expression == "amount * 2"
    assert columns["code"].unique
    assert len(child.check_constraints) == 2
    assert "amount >= 0" in child.check_constraints
    fk = child.foreign_keys[0]
    assert fk.column_ids == (
        columns["parent_b"].column_id,
        columns["parent_a"].column_id,
    )
    assert fk.referenced_column_ids == parent.primary_key
    assert fk.inspection is not None and fk.inspection.on_delete == "CASCADE"
    assert child.inspection is not None
    indexes = {index.name: index for index in child.inspection.indexes}
    assert indexes["child_amount"].keys[0].descending
    assert indexes["child_positive"].predicate == "amount > 0"
    assert indexes["child_expression"].keys[0].expression == "lower(code)"
    assert len(child.unique_constraints) == 1
    assert "private-value" not in result.model_dump_json()
    assert "database_fingerprint" not in result.model_dump()
    assert result == await adapter.inspect_metadata(request(target))


@pytest.mark.anyio
async def test_foreign_key_outside_scope_is_not_silently_removed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixture.sqlite"
    create_database(path)
    target = SQLiteTarget(path=path, target_id="fixture", include_tables=("child",))
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_CATALOG_SCOPE_INCOMPLETE"
    assert "parent" not in str(caught.value)


@pytest.mark.anyio
async def test_nullable_sqlite_primary_key_is_explicitly_unsupported(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE nullable_pk (id TEXT PRIMARY KEY)")
    target = SQLiteTarget(
        path=path, target_id="fixture", include_tables=("nullable_pk",)
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect_metadata(request(target))
    assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"
