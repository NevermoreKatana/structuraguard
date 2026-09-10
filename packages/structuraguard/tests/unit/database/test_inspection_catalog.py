"""Полный inspection port, стабильность реальной SQLite schema и CPU limits."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from tests.contract_suites.database import (
    assert_execute_unavailable,
    assert_inspection_contract,
)

from structuraguard.contracts import (
    DatabaseInspectionRequest,
    DatabaseMetadataSnapshot,
)
from structuraguard.database import (
    InspectionLimits,
    SQLiteDatabaseAdapter,
    SQLiteTarget,
    _catalog,
)
from structuraguard.database._inspection import InspectionControl
from structuraguard.domain import (
    database_fingerprint,
    verify_database_fingerprint,
)
from structuraguard.domain._database_catalog import CatalogInput
from structuraguard.exceptions import (
    DatabaseInspectionError,
)
from structuraguard.ports.database import DatabaseAdapter


def request(target: SQLiteTarget) -> DatabaseInspectionRequest:
    return DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint
    )


def target_for(path: Path) -> SQLiteTarget:
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent);
        """)
    return SQLiteTarget(path=path, target_id="test", include_tables=("child", "parent"))


@pytest.mark.anyio
async def test_full_catalog_contract_and_data_vs_schema_drift(tmp_path: Path) -> None:
    target = target_for(tmp_path / "catalog.sqlite")
    adapter: DatabaseAdapter = SQLiteDatabaseAdapter(target)
    before = await assert_inspection_contract(adapter, request(target))
    assert before.dependency_graph is not None
    ids = {table.name: table.table_id for table in before.schemas[0].tables}
    assert before.dependency_graph.load_order == (ids["parent"], ids["child"])
    with sqlite3.connect(target.path) as connection:
        connection.execute("INSERT INTO parent VALUES (1)")
    verify_database_fingerprint(
        await adapter.inspect(request(target)), before.database_fingerprint
    )
    with sqlite3.connect(target.path) as connection:
        connection.execute("ALTER TABLE child ADD COLUMN extra TEXT DEFAULT 'value'")
    after = await adapter.inspect(request(target))
    with pytest.raises(DatabaseInspectionError, match="измен") as caught:
        verify_database_fingerprint(after, before.database_fingerprint)
    assert caught.value.error_code == "DATABASE_SCHEMA_DRIFT"


@pytest.mark.anyio
async def test_execute_fails_before_io_or_consuming_batches(tmp_path: Path) -> None:
    target = SQLiteTarget(
        path=tmp_path / "missing.sqlite", target_id="test", include_tables=("table",)
    )

    await assert_execute_unavailable(SQLiteDatabaseAdapter(target))
    assert not target.path.exists()


@pytest.mark.anyio
async def test_implicit_index_order_and_duplicate_unnamed_fks(tmp_path: Path) -> None:
    results = []
    for index, unique in enumerate(("UNIQUE(a), UNIQUE(b)", "UNIQUE(b), UNIQUE(a)")):
        path = tmp_path / f"{index}.sqlite"
        with sqlite3.connect(path) as connection:
            connection.executescript(f"""
                CREATE TABLE parent(id INTEGER PRIMARY KEY);
                CREATE TABLE child(a INTEGER, b INTEGER, {unique},
                    FOREIGN KEY(a) REFERENCES parent, FOREIGN KEY(a) REFERENCES parent);
            """)
        target = SQLiteTarget(
            path=path, target_id="test", include_tables=("parent", "child")
        )
        result = await SQLiteDatabaseAdapter(target).inspect(request(target))
        results.append(result)
        assert (
            result.dependency_graph is not None
            and len(result.dependency_graph.edges) == 2
        )
    assert results[0].database_fingerprint == results[1].database_fingerprint
    assert results[0].dependency_graph == results[1].dependency_graph


@pytest.mark.anyio
async def test_cycle_catalog_does_not_publish_partial_load_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cycle.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE tree(id INTEGER PRIMARY KEY, parent INTEGER REFERENCES tree)"
        )
    target = SQLiteTarget(path=path, target_id="test", include_tables=("tree",))
    result = await SQLiteDatabaseAdapter(target).inspect(request(target))
    assert result.dependency_graph is not None
    assert result.dependency_graph.load_order is None
    assert len(result.dependency_graph.self_references) == 1


@pytest.mark.anyio
async def test_cancellation_and_busy_during_catalog_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = target_for(tmp_path / "bounded.sqlite")
    started, release = threading.Event(), threading.Event()
    original = database_fingerprint

    def delayed(catalog: CatalogInput) -> str:
        started.set()
        assert release.wait(5)
        return original(catalog)

    monkeypatch.setattr(_catalog, "database_fingerprint", delayed)
    adapter = SQLiteDatabaseAdapter(target)
    task = asyncio.create_task(adapter.inspect(request(target)))
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.01)
        with pytest.raises(DatabaseInspectionError) as caught:
            await adapter.inspect_metadata(request(target))
        assert caught.value.error_code == "DATABASE_INSPECTION_BUSY"
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await adapter.inspect(request(target))).database_fingerprint


@pytest.mark.anyio
async def test_assembly_shares_reflection_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = target_for(tmp_path / "deadline.sqlite")
    adapter = SQLiteDatabaseAdapter(target)
    snapshot = await adapter.inspect_metadata(request(target))

    ticks = iter((0.0, 0.03))

    class Clock:
        def monotonic(self) -> float:
            return next(ticks)

    monkeypatch.setattr(_catalog, "time", Clock())

    async def delayed_metadata() -> DatabaseMetadataSnapshot:
        return snapshot

    with pytest.raises(DatabaseInspectionError) as caught:
        await _catalog.inspect_catalog(
            delayed_metadata, InspectionLimits(timeout_seconds=0.01)
        )
    assert caught.value.error_code == "PROCESSING_TIMEOUT"


def test_final_catalog_with_graph_is_included_in_byte_budget(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    limits = InspectionLimits(
        max_metadata_bytes=len(catalog_snapshot.model_dump_json().encode("utf-8"))
    )
    control = InspectionControl(limits, time.monotonic() + 10)
    with pytest.raises(DatabaseInspectionError) as caught:
        _catalog._assemble(catalog_snapshot, control)
    assert caught.value.error_code == "SECURITY_LIMIT_EXCEEDED"
