from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PUBLIC_API_PATH = REPOSITORY_ROOT / "docs" / "public-api.md"
M02_CONTRACT_EXAMPLE = re.compile(
    r"<!-- example:m02-contract:start -->\s*"
    r"```python\n(?P<code>.*?)\n```\s*"
    r"<!-- example:m02-contract:end -->",
    flags=re.DOTALL,
)


def test_m02_contract_documentation_example_is_executable(tmp_path: Path) -> None:
    documentation = PUBLIC_API_PATH.read_text(encoding="utf-8")
    match = M02_CONTRACT_EXAMPLE.search(documentation)

    assert match is not None, "Копируемый M2 contract-only пример отсутствует"

    completed = subprocess.run(
        [sys.executable, "-c", match.group("code")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == (
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
