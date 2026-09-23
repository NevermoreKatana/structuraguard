"""Wire envelope M15 не должен смешивать evidence от разных исполнений."""

import asyncio

import pytest
from pydantic import ValidationError
from tests.fakes.pipeline import FakeDatabase, engine, request

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.orchestration import IngestResult


@pytest.fixture(scope="module", params=(False, True), ids=("live", "dry-run"))
def completed(request: pytest.FixtureRequest) -> IngestResult:
    from tests.fakes.pipeline import request as source_request

    result = asyncio.run(
        engine(FakeDatabase()).ingest(source_request(), dry_run=request.param)
    )
    assert result.load_report
    return result


@pytest.mark.parametrize(
    "field",
    (
        "source_fingerprint",
        "extraction_fingerprint",
        "parse_plan_fingerprint",
        "normalized_fingerprint",
        "database_fingerprint",
        "mapping_plan_fingerprint",
    ),
)
def test_envelope_rejects_foreign_fingerprint(
    completed: IngestResult, field: str
) -> None:
    wire = completed.model_dump(mode="python")
    wire[field] = canonical_sha256_value(("foreign", field))
    with pytest.raises(ValidationError):
        IngestResult.model_validate(wire)


@pytest.mark.parametrize(
    "field", ("database_fingerprint", "normalized_fingerprint", "mapping_fingerprint")
)
def test_envelope_rejects_foreign_execution_evidence(
    completed: IngestResult, field: str
) -> None:
    evidence = completed.load_result or completed.dry_run_plan
    assert evidence
    foreign = evidence.model_copy(
        update={field: canonical_sha256_value(("foreign", field))}
    )
    wire = completed.model_dump(mode="python")
    wire["dry_run_plan" if completed.dry_run else "load_result"] = foreign.model_dump(
        mode="python"
    )
    with pytest.raises(ValidationError):
        IngestResult.model_validate(wire)


def test_safe_summary_contains_no_source_canary() -> None:
    canary = "source-canary-private"
    result = asyncio.run(
        engine(FakeDatabase()).ingest(
            request(
                b'[{"field_0":"source-canary-private","field_1":"Riga"},{"field_0":"Bob","field_1":"Oslo"}]'
            ),
            dry_run=True,
        )
    )
    assert result.status.value == "COMPLETED"
    assert canary not in str(result.safe_summary())
    assert canary not in "".join(
        event.model_dump_json() for event in result.audit_events
    )
