"""Разные PYTHONHASHSEED проверяются в реальных изолированных процессах."""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = """
import asyncio
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from structuraguard.mapping import DeterministicMapper

async def main():
    db = catalog(*(table(name, column("email")) for name in {"customers", "suppliers", "staff"}))
    result = await DeterministicMapper().rank(await profile(), db, scope=scope_for(db))
    print(result.canonical_json())

asyncio.run(main())
"""


def test_hashseed_preserves_full_result_and_candidate_ids() -> None:
    package = Path(__file__).resolve().parents[3]
    outputs = []
    for seed in ("1", "987654"):
        env = os.environ.copy()
        env.update(
            PYTHONHASHSEED=seed,
            PYTHONPATH=os.pathsep.join((str(package), str(package / "src"))),
        )
        completed = subprocess.run(
            [sys.executable, "-c", SCRIPT],
            cwd=package,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not completed.stderr
        outputs.append(completed.stdout)
    assert outputs[0] == outputs[1]
    assert "candidate:" in outputs[0]
