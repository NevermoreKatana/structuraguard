from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from structuraguard.contracts.database import DatabaseInspectionRequest, TableCatalog
from structuraguard.database import (
    InspectionLimits,
    SQLiteDatabaseAdapter,
    SQLiteTarget,
)
from structuraguard.database.sqlite import _SQLiteReader
from structuraguard.exceptions import DatabaseInspectionError


@pytest.mark.anyio
async def test_inspection_does_not_create_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "absent.sqlite"
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    with pytest.raises(DatabaseInspectionError):
        await SQLiteDatabaseAdapter(target).inspect_metadata(
            DatabaseInspectionRequest(
                target_id="test", target_policy_fingerprint=target.policy_fingerprint
            )
        )
    assert not path.exists()


@pytest.mark.anyio
async def test_inspection_preserves_file_bytes_and_user_data(tmp_path: Path) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO allowed VALUES (1, 'private-value')")
    before = path.read_bytes()
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    snapshot = await SQLiteDatabaseAdapter(target).inspect_metadata(
        DatabaseInspectionRequest(
            target_id="test", target_policy_fingerprint=target.policy_fingerprint
        )
    )
    assert path.read_bytes() == before
    assert "private-value" not in snapshot.model_dump_json()
    assert tuple(tmp_path.iterdir()) == (path,)


@pytest.mark.anyio
async def test_database_file_is_opened_read_only_even_before_query_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed(id INTEGER)")
    original = sqlite3.connect
    attempts: list[int] = []

    def checked_connect(
        database: str,
        *,
        uri: bool,
        isolation_level: None,
        timeout: float,
    ) -> sqlite3.Connection:
        connection = original(
            database, uri=uri, isolation_level=isolation_level, timeout=timeout
        )
        assert connection.execute("PRAGMA query_only").fetchone() == (0,)
        for sql in (
            "CREATE TABLE mutation(id INTEGER)",
            "INSERT INTO allowed VALUES (1)",
        ):
            with pytest.raises(sqlite3.OperationalError) as caught:
                connection.execute(sql)
            attempts.append(caught.value.sqlite_errorcode)
        return connection

    monkeypatch.setattr(sqlite3, "connect", checked_connect)
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    await SQLiteDatabaseAdapter(target).inspect_metadata(
        DatabaseInspectionRequest(
            target_id="test",
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    assert attempts == [sqlite3.SQLITE_READONLY, sqlite3.SQLITE_READONLY]


@pytest.mark.anyio
async def test_metadata_connection_cannot_read_rows_or_enable_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import DatabaseError

    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed(id INTEGER)")
    original = _SQLiteReader.table
    blocked: list[str] = []

    def inspect_with_probes(reader: _SQLiteReader, name: str) -> TableCatalog:
        for sql in (
            "SELECT id FROM allowed",
            "PRAGMA query_only=OFF",
            "DROP TABLE allowed",
            "INSERT INTO allowed VALUES (1)",
            "ATTACH ':memory:' AS foreign_db",
            "SELECT load_extension('forbidden')",
        ):
            with pytest.raises(DatabaseError):
                reader.connection.exec_driver_sql(sql)
            blocked.append(sql)
        return original(reader, name)

    monkeypatch.setattr(_SQLiteReader, "table", inspect_with_probes)
    target = SQLiteTarget(path=path, target_id="test", include_tables=("allowed",))
    await SQLiteDatabaseAdapter(target).inspect_metadata(
        DatabaseInspectionRequest(
            target_id="test",
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    assert len(blocked) == 6


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [True, False])
async def test_cancel_or_deadline_joins_worker_before_returning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: bool,
) -> None:
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE allowed(id INTEGER)")
    before = path.read_bytes()
    started = threading.Event()
    stopped = threading.Event()

    def blocked_table(reader: _SQLiteReader, name: str) -> TableCatalog:
        del name
        started.set()
        try:
            assert reader.control.cancelled.wait(2)
            reader.control.check()
            raise AssertionError("cancelled inspection must fail")
        finally:
            stopped.set()

    monkeypatch.setattr(_SQLiteReader, "table", blocked_table)
    target = SQLiteTarget(
        path=path,
        target_id="test",
        include_tables=("allowed",),
        limits=InspectionLimits(timeout_seconds=1 if cancel else 0.1),
    )
    adapter = SQLiteDatabaseAdapter(target)
    request = DatabaseInspectionRequest(
        target_id="test", target_policy_fingerprint=target.policy_fingerprint
    )
    task = asyncio.create_task(adapter.inspect_metadata(request))
    assert await asyncio.to_thread(started.wait, 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(DatabaseInspectionError) as caught:
            await task
        assert caught.value.error_code == "PROCESSING_TIMEOUT"
    assert stopped.is_set()
    assert path.read_bytes() == before
    with sqlite3.connect(path, timeout=0) as connection:
        connection.execute("BEGIN EXCLUSIVE")
        connection.rollback()
