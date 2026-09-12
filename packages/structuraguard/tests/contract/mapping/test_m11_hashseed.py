"""M11 использует стабильные codes, locations и hash при разных PYTHONHASHSEED."""

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = """
import asyncio
from datetime import UTC, datetime
from tests.fakes.mapping_validation import case
from structuraguard.mapping import MappingPlanValidator

async def main():
    plan, manifest, db, policy, profile = await case()
    policy = policy.model_copy(update={"allow_schemas": tuple({"public", "unknown"}), "deny_tables": (("public", "orders"),)})
    result = await MappingPlanValidator(policy=policy, clock=lambda: datetime(2026, 9, 12, tzinfo=UTC)).validate(plan, manifest, db, profile=profile)
    print(result.canonical_json())

asyncio.run(main())
"""


def test_hashseed_does_not_change_full_m11_result() -> None:
    package = Path(__file__).resolve().parents[3]
    outputs = []
    for seed in ("1", "987654"):
        env = os.environ.copy()
        env.update(
            PYTHONHASHSEED=seed,
            PYTHONPATH=os.pathsep.join((str(package), str(package / "src"))),
        )
        result = subprocess.run(
            [sys.executable, "-c", SCRIPT],
            cwd=package,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert not result.stderr
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert "MAPPING_TABLE_DENIED" in outputs[0]
