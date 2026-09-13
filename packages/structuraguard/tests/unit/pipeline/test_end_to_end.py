"""Полный source→report с настоящими M5–M12 и fake DB boundary."""

import pytest
from tests.fakes.pipeline import FakeDatabase, engine, request

from structuraguard.contracts.common import PipelineStatus, TransactionOutcome
from structuraguard.contracts.orchestration import IngestResult


@pytest.mark.anyio
@pytest.mark.parametrize("dry_run", (True, False))
async def test_complete_pipeline_and_dry_run(dry_run: bool) -> None:
    db = FakeDatabase()
    result = await engine(db).ingest(request(), dry_run=dry_run)
    assert result.status is PipelineStatus.COMPLETED, result.errors
    assert result.source_fingerprint and result.parse_plan and result.mapping_plan
    assert result.validation_report and result.validation_report.complete
    assert result.load_report is not None
    assert result.load_report.loaded_records == (0 if dry_run else 2)
    assert db.writes == (0 if dry_run else 1)
    assert db.artifacts == [] if dry_run else len(db.artifacts) == 1
    assert IngestResult.model_validate_json(result.model_dump_json()) == result
    assert len({e.event_id for e in result.audit_events}) == len(result.audit_events)
    assert [
        e.status.value for e in result.audit_events if e.event_type == "stage_started"
    ] == [
        "SOURCE_PROBING",
        "TECHNICAL_PARSING",
        "STRUCTURE_PROFILING",
        "STRUCTURE_ANALYZING",
        "PARSE_PLAN_CREATED",
        "PARSE_PLAN_VALIDATING",
        "SEMANTIC_PARSING",
        "NORMALIZED_DATA_PROFILING",
        "DATABASE_INSPECTING",
        "MAPPING",
        "MAPPING_PLAN_CREATED",
        "MAPPING_PLAN_VALIDATING",
        "NORMALIZING",
        "VALIDATING",
        "STAGING",
        "LOADING",
    ]


@pytest.mark.anyio
async def test_loader_error_preserves_confirmed_rollback() -> None:
    db = FakeDatabase()
    db.error = "LOAD_CONSTRAINT_FAILED"
    result = await engine(db).ingest(request())
    assert result.status is PipelineStatus.ROLLED_BACK, result.errors
    assert result.transaction_outcome is TransactionOutcome.ROLLED_BACK
    assert result.errors[-1].code == "LOAD_CONSTRAINT_FAILED"
    assert db.writes == 0
