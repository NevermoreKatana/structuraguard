"""Публичный пример provenance выполняется на реальных M5 artifacts без сети."""

import re
import subprocess
import sys
from pathlib import Path


def test_m12_provenance_example_is_copyable(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/provenance-validation.md").read_text()
    match = re.search(
        r"<!-- example:m12-provenance:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    script = tmp_path / "provenance_example.py"
    guard = f"""
import sys
sys.path.insert(0, {str(root / "packages/structuraguard")!r})
def deny_network(event, args):
    if event in {{'socket.connect', 'socket.getaddrinfo'}}:
        raise AssertionError('Network forbidden in provenance example')
sys.addaudithook(deny_network)
"""
    driver = """
import asyncio
from datetime import UTC
from tests.fakes.provenance import provenance_fixture
from structuraguard.contracts.common import ValidationDecision
async def main():
    fixture = await provenance_fixture()
    report = await validate_snapshot(fixture.normalized, fixture.physical, fixture.plan, fixture.context, datetime(2026, 9, 12, tzinfo=UTC))
    assert report.decision is ValidationDecision.ACCEPTED
    assert report.valid_records == 2
asyncio.run(main())
"""
    script.write_text(guard + match.group("code") + driver)
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_m12_report_example_blocks_missing_database_check(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs/provenance-validation.md").read_text()
    match = re.search(
        r"<!-- example:m12-report:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        re.DOTALL,
    )
    assert match is not None
    guard = f"""
import sys
sys.path.insert(0, {str(root / "packages/structuraguard")!r})
def deny_network(event, args):
    if event in {{'socket.connect', 'socket.getaddrinfo'}}:
        raise AssertionError('Network forbidden in report example')
sys.addaudithook(deny_network)
"""
    driver = """
import asyncio
from datetime import UTC, datetime
from tests.fakes.provenance import provenance_fixture
from structuraguard.contracts.common import ValidationDecision
from structuraguard.validation import ProvenanceValidator
async def main():
    fixture = await provenance_fixture()
    original = await ProvenanceValidator().validate(
        fixture.normalized, source_batches=fixture.physical, plan=fixture.plan,
        context=fixture.context, generated_at=datetime(2026, 9, 13, tzinfo=UTC),
    )
    before = original.canonical_json()
    assert original.decision is ValidationDecision.ACCEPTED
    assert original.complete
    report = require_database_check(original)
    assert report.decision is ValidationDecision.NEEDS_REVIEW
    assert not report.complete
    assert [finding.code for finding in report.findings] == ['VALIDATION_LAYER_UNVERIFIED']
    assert report.findings[0].layer is ValidationLayer.DATABASE
    assert report.evidence == original.evidence
    assert original.canonical_json() == before
asyncio.run(main())
"""
    script = tmp_path / "report_example.py"
    script.write_text(guard + match.group("code") + driver)
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
