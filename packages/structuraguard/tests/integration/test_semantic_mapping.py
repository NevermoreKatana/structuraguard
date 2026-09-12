"""SQLite inspection → M8 → M9 → Fake LLM → M10 без изменения файла БД."""

import sqlite3
from pathlib import Path

import pytest
from tests.fakes.mapping import profile, scope_for
from tests.fakes.semantic_mapping import decision_for, mapping_options, run_mapper

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.mapping import prepare_semantic_mapping

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_sqlite_profile_candidates_semantic_proposal_never_write(
    tmp_path: Path,
) -> None:
    path = tmp_path / "semantic.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE customers (email TEXT)")
    before = path.read_bytes()
    target = SQLiteTarget(
        path=path, target_id="semantic", include_tables=("customers",)
    )
    db = await SQLiteDatabaseAdapter(target).inspect(
        DatabaseInspectionRequest(
            target_id=target.target_id,
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )
    data = await profile()
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    result, provider, _ = await run_mapper(data, db, decision_for(prepared.groups[0]))
    assert result.groups[0].decision is not None
    assert provider.call_count == 1
    assert path.read_bytes() == before
