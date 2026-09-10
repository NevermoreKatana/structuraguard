"""PostgreSQL-пример из документации работает с реальным inspector и constraints."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from structuraguard.database import PostgreSQLTarget

pytestmark = [pytest.mark.integration, pytest.mark.database_integration]


def test_documented_postgresql_example(
    pg_target: PostgreSQLTarget, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[5]
    documentation = (root / "docs/database-inspection.md").read_text(encoding="utf-8")
    match = re.search(
        r"<!-- example:m07-postgresql:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    assertions = (
        "\nimport asyncio, sys\n"
        "snapshot = asyncio.run(inspect_postgresql(SecretStr(sys.stdin.read())))\n"
        "assert snapshot.schema_version == '1.1.0'\n"
        "assert snapshot.comments_supported\n"
        "assert snapshot.write_permissions == 'unknown'\n"
        "tables = {(s.name, t.name): t for s in snapshot.schemas for t in s.tables}\n"
        "assert set(tables) == {('app', 'items'), ('ref', 'parents')}\n"
        "assert tables[('app', 'items')].foreign_keys[0].referenced_table_id == tables[('ref', 'parents')].table_id\n"
        "assert 'row-canary-never-read' not in snapshot.model_dump_json()\n"
    )
    # Fixture DSN передаётся через stdin, чтобы не попадать в argv/process list.
    result = subprocess.run(
        [sys.executable, "-c", match.group("code") + assertions],
        input=pg_target.dsn.get_secret_value(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == ""
