from __future__ import annotations

import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _job_body(workflow: str, job_name: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(job_name)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
        workflow,
    )
    if match is None:
        raise AssertionError(f"CI job {job_name!r} отсутствует")
    return match.group("body")


def test_ci_enforces_locked_read_only_m01_quality_gates() -> None:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert re.search(r"(?m)^permissions:\n  contents: read$", workflow)
    assert re.search(r"(?i)\bsecrets\b", workflow) is None
    assert re.search(r"(?m)^\s+[a-z-]+:\s+write\s*$", workflow) is None
    assert "write-all" not in workflow
    assert workflow.count("persist-credentials: false") == 4

    action_references = re.findall(r"(?m)^\s+uses:\s+(\S+)", workflow)
    assert action_references
    assert all(
        re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", reference)
        for reference in action_references
    )

    quality = _job_body(workflow, "quality")
    assert 'python-version: "3.12"' in quality
    assert "uv lock --check" in quality
    assert "uv sync --all-packages --locked --group dev --group docs" in quality
    assert "make lint typecheck docs" in quality

    tests = _job_body(workflow, "tests")
    assert 'python-version: ["3.12", "3.13", "3.14"]' in tests
    assert "uv lock --check" in tests
    assert "uv sync --all-packages --locked --group dev" in tests
    assert "make test" in tests

    package = _job_body(workflow, "package")
    assert 'python-version: "3.12"' in package
    assert "uv lock --check" in package
    assert "uv sync --all-packages --locked --group dev" in package
    assert "make test-build" in package

    database = _job_body(workflow, "database")
    assert 'python-version: "3.12"' in database
    assert "uv lock --check" in database
    assert (
        "uv sync --all-packages --locked --group dev --extra postgres --extra sqlite"
        in database
    )
    assert "docker info" in database
    assert "make test-database" in database
