from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROBE_PATH = Path(__file__).with_name("import_probe.py")
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("mode", ["black-box", "attribution"])
def test_import_has_no_io_or_process_side_effects(
    mode: str,
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", "-I", str(PROBE_PATH), mode, str(SOURCE_ROOT)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, (
        f"{mode} probe failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
