"""Копируемый SDK-пример выполняется без БД, модели и сети."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("example_index", (0, 1), ids=("async", "sync"))
def test_m15_source_to_profile_example(example_index: int, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    document = (root / "docs/sdk-orchestrator.md").read_text()
    examples = re.findall(r"```python\n(.*?)\n```", document, re.DOTALL)
    assert len(examples) == 2
    script = tmp_path / "example.py"
    guard = """
import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Сеть запрещена в примере M15')
sys.addaudithook(deny_network)
"""
    script.write_text(guard + examples[example_index], encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "records=2\n"
    assert not completed.stderr
