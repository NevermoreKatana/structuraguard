from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import (
    InspectionLimits,
    SQLiteDatabaseAdapter,
    SQLiteTarget,
)
from structuraguard.exceptions import DatabaseInspectionError


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["target", "fingerprint", "schema", "empty", "system", "columns", "limit"]
)
async def test_policy_rejection_happens_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        raise AssertionError("connection must not be opened")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    target = SQLiteTarget(
        path=tmp_path / "not-created.sqlite",
        target_id="test",
        include_tables=()
        if case == "empty"
        else ("sqlite_master",)
        if case == "system"
        else ("a", "b"),
        deny_columns=("private",) if case == "columns" else (),
        limits=InspectionLimits(max_tables=1 if case == "limit" else 256),
    )
    with pytest.raises(DatabaseInspectionError):
        await SQLiteDatabaseAdapter(target).inspect_metadata(
            DatabaseInspectionRequest(
                target_id="different" if case == "target" else "test",
                target_policy_fingerprint="sha256:" + "f" * 64
                if case == "fingerprint"
                else target.policy_fingerprint,
                schema_names=("temp",) if case == "schema" else (),
            )
        )


@pytest.mark.anyio
@pytest.mark.parametrize("denied", ["forbidden", "FORBIDDEN"])
async def test_denylist_applies_before_any_object_reflection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    denied: str,
) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE allowed(id INTEGER); CREATE TABLE forbidden(secret TEXT);"
        )
    original_connect = sqlite3.connect
    statements: list[str] = []

    def traced_connect(
        database: str, *, uri: bool, isolation_level: None, timeout: float
    ) -> sqlite3.Connection:
        connection = original_connect(
            database, uri=uri, isolation_level=isolation_level, timeout=timeout
        )
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    target = SQLiteTarget(
        path=path,
        target_id="test",
        include_tables=("allowed", "forbidden"),
        deny_tables=(denied,),
    )
    snapshot = await SQLiteDatabaseAdapter(target).inspect_metadata(
        DatabaseInspectionRequest(
            target_id="test",
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    assert [table.name for table in snapshot.schemas[0].tables] == ["allowed"]
    assert statements
    assert not any("forbidden" in sql for sql in statements)
    assert not any(
        sql.upper().startswith(
            ("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER", "DROP")
        )
        for sql in statements
    )


@pytest.mark.anyio
async def test_metadata_limits_fail_without_partial_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed(a TEXT, b TEXT)")
    target = SQLiteTarget(
        path=path,
        target_id="test",
        include_tables=("allowed",),
        limits=InspectionLimits(max_columns=1),
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect_metadata(
            DatabaseInspectionRequest(
                target_id="test",
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
    assert caught.value.error_code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_debug_logging_and_errors_never_expose_metadata(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "private-path.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE allowed(value TEXT DEFAULT 'password=private-canary')"
        )
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(DatabaseInspectionError) as caught,
    ):
        await SQLiteDatabaseAdapter(target).inspect_metadata(
            DatabaseInspectionRequest(
                target_id="test",
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
    assert "private-canary" not in str(caught.value)
    assert "private-path" not in str(caught.value)
    assert "private-canary" not in caplog.text
    assert "CREATE TABLE" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "limit", ["max_text_chars", "max_items", "max_metadata_bytes", "max_constraints"]
)
async def test_all_metadata_budgets_are_enforced(tmp_path: Path, limit: str) -> None:
    path = tmp_path / "limits.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE allowed(value TEXT CHECK(length(value)>0) CHECK(value<>'bad'))"
        )
    target = SQLiteTarget(
        path=path,
        target_id="test",
        include_tables=("allowed",),
        limits=InspectionLimits.model_validate({limit: 1}),
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        await SQLiteDatabaseAdapter(target).inspect_metadata(
            DatabaseInspectionRequest(
                target_id="test",
                target_policy_fingerprint=target.policy_fingerprint,
            )
        )
    assert caught.value.error_code == "SECURITY_LIMIT_EXCEEDED"
