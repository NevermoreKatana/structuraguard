"""Исходы loader boundary: отсутствие ответа не доказывает отсутствие DML."""

import asyncio
from dataclasses import replace

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import FakeDatabase, defaults, engine, request
from tests.unit.pipeline.test_acceptance_lifecycle import repeatable

from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts import TransactionOutcome
from structuraguard.contracts.loading import LoadRequest, PostgreSQLLoadResult
from structuraguard.contracts.staging import StagingRetentionPolicy, StagingRunStatus
from structuraguard.exceptions import LoadError
from structuraguard.stores import MemoryStagingStore


class LostResponseDatabase(FakeDatabase):
    def __init__(self, status: StagingRunStatus) -> None:
        super().__init__()
        self.status = status
        self.attempts = 0

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        self.attempts += 1
        run = await self.store.get_run(request.staging_context)
        run = await self.store.transition(
            request.staging_context,
            expected_revision=run.revision,
            status=StagingRunStatus.EXECUTING,
        )
        if self.status is not StagingRunStatus.EXECUTING:
            await self.store.transition(
                request.staging_context,
                expected_revision=run.revision,
                status=self.status,
            )
        raise LoadError(
            error_code="LOAD_CONNECTION_FAILED", message="secret-DSN-canary"
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status", (StagingRunStatus.EXECUTING, StagingRunStatus.UNKNOWN)
)
async def test_missing_loader_outcome_remains_unknown_without_retry(
    status: StagingRunStatus,
) -> None:
    db = LostResponseDatabase(status)
    result = await engine(db).ingest(request())
    assert result.status is S.FAILED
    assert result.transaction_outcome is TransactionOutcome.UNKNOWN
    assert result.errors[-1].code == "LOAD_CONNECTION_FAILED"
    assert result.load_report is None and result.load_result is None
    assert db.attempts == 1
    assert "secret-DSN-canary" not in result.model_dump_json()


@pytest.mark.anyio
async def test_missing_load_dependencies_never_stage() -> None:
    db = FakeDatabase()
    sdk = engine(
        dependencies=replace(
            defaults(db),
            database=lambda resources: replace(db.bind(resources), loader=None),
        )
    )
    result = await sdk.ingest(request())
    assert result.status is S.NEEDS_REVIEW
    assert result.errors[-1].code == "SDK_LOAD_DEPENDENCY_REQUIRED"
    assert not db.writes and not db.artifacts
    assert result.validation_report and result.validation_report.complete


class CommittedResponseLost(FakeDatabase):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure
        self.store = MemoryStagingStore(
            target_id=self.catalog.target_id,
            retention=StagingRetentionPolicy(),
            clock=fixed_clock,
        )

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        await super().execute(request)
        if self.failure == "cancel":
            raise asyncio.CancelledError
        if self.failure == "runtime":
            raise RuntimeError("private-backend-canary")
        raise LoadError(error_code=self.failure, message="private-backend-canary")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,warning",
    (
        ("LOAD_CONNECTION_FAILED", "LOAD_CONNECTION_FAILED"),
        ("LOAD_OUTCOME_UNKNOWN", "LOAD_OUTCOME_UNKNOWN"),
        ("runtime", "SDK_STAGE_FAILED"),
        ("cancel", "LOAD_CANCELLED_AFTER_COMMIT"),
    ),
)
async def test_committed_staging_preserves_outcome_without_loader_result(
    failure: str, warning: str
) -> None:
    db = CommittedResponseLost(failure)
    result = await engine(dependencies=repeatable(db)).ingest(request())
    assert db.writes == 1 and len(db.artifacts) == 1
    assert result.status is S.COMPLETED_WITH_WARNINGS
    assert result.transaction_outcome is TransactionOutcome.COMMITTED
    assert warning in result.warnings
    assert result.load_result is None and result.load_report is None
    assert "private-backend-canary" not in result.model_dump_json()
