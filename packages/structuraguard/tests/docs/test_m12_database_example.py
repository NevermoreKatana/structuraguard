"""Публичный пример DB prechecks запускается на настоящей локальной SQLite."""

import re
import subprocess
import sys
from pathlib import Path


def test_m12_database_example_is_copyable_and_read_only(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/db-business-validation.md").read_text()
    match = re.search(
        r"<!-- example:m12-database:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    guard = f"""
import sys
sys.path.insert(0, {str(root / "packages/structuraguard")!r})
def deny_network(event, args):
    if event in {{'socket.connect', 'socket.getaddrinfo'}}:
        raise AssertionError('Network forbidden in SQLite example')
sys.addaudithook(deny_network)
"""
    driver = """
import asyncio
import sqlite3
from pathlib import Path
from tests.fakes.mapping import refs
from tests.unit.validation.test_db_constraints import data, integer, policy
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.database import SQLiteDatabaseAdapter
async def main():
    path = Path(sys.argv[1])
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE items(a INTEGER NOT NULL UNIQUE)')
        connection.execute('INSERT INTO items VALUES(1)')
    target = SQLiteTarget(path=path, target_id='example', include_tables=('items',))
    catalog = await SQLiteDatabaseAdapter(target).inspect(DatabaseInspectionRequest(
        target_id=target.target_id, target_policy_fingerprint=target.policy_fingerprint,
    ))
    table = catalog.schemas[0].tables[0]
    dataset = data((table.table_id, {table.columns[0].column_id: integer(1)}))
    before = dataset.canonical_json()
    result = await validate_database(
        dataset, catalog, target, policy(catalog), ConstraintReadPolicy(allow_columns=refs(catalog)),
    )
    assert not result.accepted
    assert [issue.code for issue in result.issues] == ['DB_UNIQUE']
    assert result.read_snapshot_fingerprint is not None
    assert dataset.canonical_json() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT a FROM items').fetchall() == [(1,)]
asyncio.run(main())
"""
    script = tmp_path / "database_example.py"
    script.write_text(guard + match.group("code") + driver)
    result = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "example.sqlite")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
