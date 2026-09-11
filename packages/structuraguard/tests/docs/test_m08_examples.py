"""Документированный M8 пример исполняется офлайн и возвращает safe summary."""

import re
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("source_classification", ["INTERNAL", "RESTRICTED"])
def test_m08_example_uses_normalized_input_offline(
    tmp_path: Path, source_classification: str
) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/normalized-profiling.md").read_text()
    match = re.search(
        r"<!-- example:m08-profile:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    guard = """import sys
def deny_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo'}:
        raise AssertionError('Network запрещена в примере M8')
sys.addaudithook(deny_network)
"""
    assertions = """
import asyncio
from tests.fakes.profiling import normalized_stream
from structuraguard.contracts.common import StringScalar

source_classification = DataClassification[sys.argv[1]]
summary = asyncio.run(summarize_normalized(
    normalized_stream([{"email": StringScalar(value="private@example.org")}]),
    source_classification=source_classification,
))
assert summary.record_count == 1
assert summary.pii_field_count == 1
expected = (
    DataClassification.RESTRICTED
    if source_classification == DataClassification.RESTRICTED
    else DataClassification.CONFIDENTIAL
)
assert summary.classification == expected
assert set(summary.model_dump()) == {
    'record_count', 'entity_count', 'value_count', 'field_count',
    'pii_field_count', 'ambiguous_field_count', 'classification',
}
assert 'private' not in summary.canonical_json()
"""
    import_path = (
        f"import sys\nsys.path.insert(0, {str(root / 'packages/structuraguard')!r})\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            import_path + guard + match.group("code") + assertions,
            source_classification,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
