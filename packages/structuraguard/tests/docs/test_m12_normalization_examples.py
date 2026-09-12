"""Копируемые примеры built-in и custom normalization выполняются без сети."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("marker", ["m12-normalization", "m12-custom-normalizer"])
def test_m12_normalization_example_is_copyable_offline(
    marker: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/normalization.md").read_text()
    match = re.search(
        rf"<!-- example:{marker}:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    guard = """
import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden in normalization example')
sys.addaudithook(deny_network)
"""
    script = tmp_path / "normalization_example.py"
    script.write_text(guard + match.group("code"))
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
