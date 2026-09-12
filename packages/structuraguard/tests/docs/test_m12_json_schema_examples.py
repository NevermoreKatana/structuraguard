"""Копируемый пример JSON Schema запускается без сети."""

import re
import subprocess
import sys
from pathlib import Path


def test_m12_schema_documented_example_is_copyable_offline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/json-schema-validation.md").read_text()
    match = re.search(
        r"<!-- example:m12-schema:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    script = tmp_path / "schema_example.py"
    guard = """
import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network forbidden in schema example')
sys.addaudithook(deny_network)
"""
    script.write_text(guard + match.group("code"))
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
