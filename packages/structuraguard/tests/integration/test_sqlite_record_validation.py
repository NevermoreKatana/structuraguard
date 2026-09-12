"""Настоящие SQLite UNIQUE/FK prechecks без изменения строк."""

import sqlite3
from pathlib import Path

import pytest
from tests.fakes.mapping import refs
from tests.unit.validation.test_db_constraints import data, integer, policy

from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.database.constraint_reader import DatabaseConstraintReader
from structuraguard.validation import DatabaseConstraintValidator

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


async def test_existing_composite_keys_and_fk_use_one_read_only_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "keys.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE parents(a INTEGER NOT NULL, b INTEGER NOT NULL, UNIQUE(b,a)); CREATE TABLE children(x INTEGER, y INTEGER, FOREIGN KEY(y,x) REFERENCES parents(b,a)); INSERT INTO parents VALUES(1,2);"
        )
    target = SQLiteTarget(
        path=path, target_id="keys", include_tables=("parents", "children")
    )
    db = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    tables = {t.name: t for s in db.schemas for t in s.tables}
    parent, child = tables["parents"], tables["children"]
    pcols = {c.name: c.column_id for c in parent.columns}
    ccols = {c.name: c.column_id for c in child.columns}
    source = data(
        (parent.table_id, {pcols["a"]: integer(1), pcols["b"]: integer(2)}),
        (child.table_id, {ccols["x"]: integer(3), ccols["y"]: integer(4)}),
    )
    reader = DatabaseConstraintReader(
        target, policy=ConstraintReadPolicy(allow_columns=refs(db))
    )
    report = await DatabaseConstraintValidator(policy(db), reader=reader).validate(
        source, catalog=db
    )
    assert [i.code for i in report.issues] == ["DB_UNIQUE", "DB_FOREIGN_KEY"]
    assert report.read_snapshot_fingerprint is not None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT a,b FROM parents").fetchall() == [(1, 2)]
        assert connection.execute("SELECT count(*) FROM children").fetchone() == (0,)
