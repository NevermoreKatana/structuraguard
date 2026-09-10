"""Regression: цепочки ошибок и непредставимые SQLite schema semantics."""

from __future__ import annotations

import logging
import sqlite3
import traceback
from pathlib import Path

import pytest
from pydantic import SecretStr

from structuraguard.contracts import DatabaseInspectionRequest
from structuraguard.database import (
    PostgreSQLDatabaseAdapter,
    PostgreSQLTarget,
    SQLiteDatabaseAdapter,
    SQLiteTarget,
)
from structuraguard.exceptions import DatabaseInspectionError


@pytest.mark.anyio
@pytest.mark.parametrize("full_catalog", [False, True])
async def test_sqlite_failure_drops_raw_exception_context(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, full_catalog: bool
) -> None:
    path = tmp_path / "private-path.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            'CREATE VIEW allowed AS SELECT * FROM "restricted-customer-canary"'
        )
    before = path.read_bytes()
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    adapter = SQLiteDatabaseAdapter(target)
    inspect = adapter.inspect if full_catalog else adapter.inspect_metadata
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(DatabaseInspectionError) as caught,
    ):
        await inspect(
            DatabaseInspectionRequest(
                target_id="test", target_policy_fingerprint=target.policy_fingerprint
            )
        )
    assert caught.value.error_code == "DATABASE_INSPECTION_FAILED"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    output = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert "restricted-customer-canary" not in output + caplog.text
    assert str(path) not in output + caplog.text
    assert path.read_bytes() == before
    with sqlite3.connect(path, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()


@pytest.mark.anyio
@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("full_catalog", [False, True])
async def test_rejected_request_has_no_validation_exception_chain(
    tmp_path: Path, dialect: str, full_catalog: bool
) -> None:
    adapter: SQLiteDatabaseAdapter | PostgreSQLDatabaseAdapter
    if dialect == "sqlite":
        sqlite_target = SQLiteTarget(
            path=tmp_path / "absent.sqlite",
            target_id="test",
            include_tables=("allowed",),
        )
        adapter = SQLiteDatabaseAdapter(sqlite_target)
        policy = sqlite_target.policy_fingerprint
    else:
        pg_target = PostgreSQLTarget(
            dsn=SecretStr("postgresql+asyncpg://inspector:canary@localhost/db"),
            target_id="test",
            include_schemas=("app",),
            include_tables=(("app", "allowed"),),
        )
        adapter = PostgreSQLDatabaseAdapter(pg_target)
        policy = pg_target.policy_fingerprint
    request = DatabaseInspectionRequest(
        target_id="test", target_policy_fingerprint=policy
    ).model_copy(update={"schema_names": ("password=request-canary",)})
    inspect = adapter.inspect if full_catalog else adapter.inspect_metadata
    with pytest.raises(DatabaseInspectionError) as caught:
        await inspect(request)
    assert caught.value.error_code == "TARGET_NOT_ALLOWED"
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    "definition",
    [
        "id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) "
        "DEFERRABLE INITIALLY DEFERRED",
        "id INTEGER PRIMARY KEY, parent_id INTEGER, FOREIGN KEY(parent_id) "
        "REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED",
        "id INTEGER PRIMARY KEY ON CONFLICT IGNORE, parent_id INTEGER REFERENCES parent(id)",
        "id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), "
        "value TEXT COLLATE NOCASE",
    ],
)
async def test_unrepresented_sqlite_clauses_fail_closed(
    tmp_path: Path, definition: str
) -> None:
    path = tmp_path / "drift.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE child(" + definition + ")")
    before = path.read_bytes()
    target = SQLiteTarget(
        path=path, target_id="test", include_tables=("parent", "child")
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id="test", target_policy_fingerprint=target.policy_fingerprint
            )
        )
    assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_sqlite_clause_words_in_literals_comments_and_expressions_are_data(
    tmp_path: Path,
) -> None:
    path = tmp_path / "literal.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE allowed(\"COLLATE\" TEXT DEFAULT 'DEFERRABLE ON CONFLICT', "
            "value TEXT CHECK(value COLLATE NOCASE <> 'bad')) "
            "/* DEFERRABLE ON CONFLICT COLLATE */"
        )
        connection.execute("CREATE INDEX ix ON allowed(value COLLATE NOCASE)")
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    catalog = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id="test", target_policy_fingerprint=target.policy_fingerprint
        )
    )
    table = catalog.schemas[0].tables[0]
    assert table.check_constraints == ("value COLLATE NOCASE <> 'bad'",)
    assert table.inspection is not None
    assert table.inspection.indexes[0].keys[0].collation == "NOCASE"


@pytest.mark.anyio
async def test_malformed_sqlite_file_is_rejected_without_leaking_bytes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "malformed.sqlite"
    payload = b"not-a-database restricted-file-canary\x00" * 16
    path.write_bytes(payload)
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(DatabaseInspectionError) as caught,
    ):
        await SQLiteDatabaseAdapter(target).inspect(
            DatabaseInspectionRequest(
                target_id="test", target_policy_fingerprint=target.policy_fingerprint
            )
        )
    assert caught.value.error_code == "DATABASE_INSPECTION_FAILED"
    assert caught.value.__context__ is None
    assert "restricted-file-canary" not in repr(caught.value) + caplog.text
    assert path.read_bytes() == payload
    assert tuple(tmp_path.iterdir()) == (path,)


@pytest.mark.anyio
async def test_sqlite_path_cannot_supply_uri_options(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite?mode=rw&immutable=1#fragment"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed(id INTEGER)")
    before = path.read_bytes()
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    catalog = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id="test", target_policy_fingerprint=target.policy_fingerprint
        )
    )
    assert catalog.schemas[0].tables[0].name == "allowed"
    assert path.read_bytes() == before
    assert tuple(tmp_path.iterdir()) == (path,)
