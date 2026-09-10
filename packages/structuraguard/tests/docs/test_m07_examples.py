"""Пример SQLite inspection выполняется на локальной fixture без сети."""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("example", "function", "catalog"),
    [
        ("m07-inspection", "inspect_schema", False),
        ("m07-catalog", "inspect_catalog", True),
    ],
)
def test_sqlite_documented_example(
    tmp_path: Path, example: str, function: str, catalog: bool
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/database-inspection.md").read_text()
    match = re.search(
        rf"<!-- example:{example}:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    path = tmp_path / "fixture.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE customers(id INTEGER PRIMARY KEY);"
            "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers);"
            "CREATE TABLE private_notes(private TEXT);"
        )
    guard = (
        "import sys\n"
        "def no_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Network запрещена в примере M7')\n"
        "sys.addaudithook(no_network)\n"
    )
    assertions = (
        "\nimport asyncio\n"
        f"snapshot = asyncio.run({function}(Path(sys.argv[1])))\n"
        "assert [t.name for t in snapshot.schemas[0].tables] == ['customers', 'orders']\n"
    )
    assertions += (
        "assert snapshot.database_fingerprint.startswith('sha256:')\n"
        "assert len(snapshot.dependency_graph.load_order) == 2\n"
        if catalog
        else "assert 'database_fingerprint' not in snapshot.model_dump()\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", guard + match.group("code") + assertions, str(path)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
