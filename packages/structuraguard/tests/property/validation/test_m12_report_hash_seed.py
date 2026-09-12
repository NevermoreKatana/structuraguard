"""M12 AC-07: report hash не зависит от process hash randomization."""

import os
import subprocess
import sys
from pathlib import Path


def test_report_hash_is_stable_across_hash_seeds_and_set_iteration(
    tmp_path: Path,
) -> None:
    package = Path(__file__).resolve().parents[3]
    script = tmp_path / "hash_report.py"
    script.write_text(
        f"""
import sys
sys.path.insert(0, {str(package)!r})
"""
        + """
import asyncio
from datetime import UTC, datetime
from tests.fakes.provenance import provenance_fixture
from structuraguard.contracts.provenance import ValidationFinding, ValidationLayer, ValidationLayerResult
from structuraguard.validation import ProvenanceValidator, ValidationReportBuilder

async def main():
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(fixture.normalized, source_batches=fixture.physical, plan=fixture.plan, context=fixture.context, generated_at=datetime(2026,9,13,tzinfo=UTC))
    findings = tuple(ValidationFinding(layer=ValidationLayer.JSON_SCHEMA, code=code, record_index=record, json_path=path) for code,record,path in {('SCHEMA_TYPE',1,'$.я'),('SCHEMA_TYPE',0,'$.a'),('SCHEMA_REQUIRED',0,'$.z')})
    layer = ValidationLayerResult(layer=ValidationLayer.JSON_SCHEMA, input_fingerprint=report.lineage.input_fingerprint, evidence_fingerprints=('sha256:'+'f'*64,), complete=True, findings=findings)
    final = ValidationReportBuilder(required_layers=(ValidationLayer.JSON_SCHEMA,)).combine(report,layers=(layer,))
    print(final.evidence_fingerprint)
asyncio.run(main())
"""
    )
    fingerprints = []
    for seed in ("1", "777"):
        completed = subprocess.run(
            [sys.executable, str(script)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        fingerprints.append(completed.stdout.strip())
    assert fingerprints[0].startswith("sha256:")
    assert fingerprints[0] == fingerprints[1]
