"""Staging и M13 execution; facade не создаёт SQL и не повторяет DML."""

import asyncio
from datetime import timedelta

from structuraguard.contracts.common import (
    IssueSeverity,
    TransactionOutcome,
    ValidationIssue,
)
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.loading import DryRunRequest, LoadRequest
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.reports import LoadReport
from structuraguard.contracts.security import Resource
from structuraguard.contracts.staging import StagingRunSpec, StagingRunStatus

from .session import producer
from .source import NormalizedData, classify_source


def report(data: NormalizedData, checked: ValidatedMappingPlan) -> LoadReport:
    run = data.source._run
    plan = checked.plan
    result = run.result.load_result
    forecast = run.result.dry_run_plan
    rejected = (
        result.rejected_records
        if result
        else len(
            {step.record_id for step in forecast.steps if step.action == "quarantine"}
        )
        if forecast
        else 0
    )
    issues = tuple(
        ValidationIssue(code=code, message_key=code, severity=IssueSeverity.WARNING)
        for code in run.result.warnings
    )
    return LoadReport(
        run_id=run.run_id,
        producer=producer(),
        status=S.COMPLETED_WITH_WARNINGS if rejected or issues else S.COMPLETED,
        operation=plan.operation,
        transaction_outcome=run.result.transaction_outcome,
        dry_run=run.result.dry_run,
        source_fingerprint=plan.source_fingerprint,
        extraction_fingerprint=plan.extraction_fingerprint,
        parse_plan_fingerprint=plan.parse_plan_fingerprint,
        normalized_fingerprint=plan.normalized_fingerprint,
        mapping_plan_fingerprint=plan.fingerprint,
        mapping_validation_fingerprint=checked.validation_fingerprint,
        database_fingerprint=plan.database_fingerprint,
        target_id=plan.target_id,
        target_policy_fingerprint=plan.target_policy_fingerprint,
        artifact_fingerprints=tuple(
            dict.fromkeys(
                (
                    plan.source_fingerprint,
                    plan.extraction_fingerprint,
                    plan.parse_plan_fingerprint,
                    plan.normalized_fingerprint,
                    plan.fingerprint,
                    checked.validation_fingerprint,
                    plan.database_fingerprint,
                    plan.target_policy_fingerprint,
                )
            )
        ),
        attempted_records=data.manifest.record_count,
        loaded_records=result.loaded_records if result else 0,
        would_load_records=data.manifest.record_count - rejected
        if run.result.dry_run
        else 0,
        rejected_records=rejected,
        issues=issues,
        generated_at=run.dependencies.clock(),
    )


async def load(
    data: NormalizedData, checked: ValidatedMappingPlan, *, idempotency_key: str | None
) -> None:
    run = data.source._run
    binding = run.database
    assert binding is not None
    request = DryRunRequest(batches=data.batches, mapping=checked.plan)
    context: StagingContext | None = None

    async def stage() -> LoadRequest | None:
        nonlocal context
        run.resources.check_deadline()
        scans = await classify_source(data.source)
        run.set(
            security_report=run.result.security_report.model_copy(
                update={
                    "privacy": (
                        *run.result.security_report.privacy,
                        *scans,
                    )
                }
            )
        )
        if run.result.dry_run:
            return None
        if (
            binding.loader is None
            or binding.staging is None
            or binding.references is None
        ):
            await run.stop("SDK_LOAD_DEPENDENCY_REQUIRED")
        assert binding.staging is not None and binding.references is not None
        deadline = run.dependencies.clock() + timedelta(
            seconds=run.resources.remaining_seconds(Resource.PROCESSING_TIME_MS)
        )
        context = StagingContext(
            run_id=run.run_id,
            staging_id=run.run_id,
            target_id=checked.target_id,
            database_fingerprint=checked.database_fingerprint,
            target_policy_fingerprint=checked.target_policy_fingerprint,
            normalized_fingerprint=checked.normalized_fingerprint,
            expires_at=deadline,
            max_records=run.dependencies.max_records,
        )
        references = await binding.references(request, run.run_id, deadline)
        spec = StagingRunSpec(
            context=context,
            source_fingerprint=checked.plan.source_fingerprint,
            extraction_fingerprint=checked.plan.extraction_fingerprint,
            parse_plan_fingerprint=checked.plan.parse_plan_fingerprint,
            mapping_plan_fingerprint=checked.plan.fingerprint,
            batch_count=len(data.batches),
            record_count=data.manifest.record_count,
            references=references,
        )
        await binding.staging.begin(spec)
        run.staging_context = context
        for batch in data.batches:
            await binding.staging.stage(batch, context)
        staged = await binding.staging.get_run(context)
        sealed = await binding.staging.seal(context, expected_revision=staged.revision)
        return LoadRequest(
            snapshot=request,
            staging_context=context,
            staging_revision=sealed.revision,
            idempotency_key=idempotency_key,
        )

    load_request = await run.perform(S.STAGING, stage)

    async def execute() -> None:
        if run.result.dry_run:
            prediction = await binding.planner.plan(request)
            if (
                prediction.normalized_fingerprint,
                prediction.mapping_fingerprint,
                prediction.database_fingerprint,
                prediction.target_id,
                prediction.target_policy_fingerprint,
            ) != (
                checked.normalized_fingerprint,
                checked.plan.fingerprint,
                checked.database_fingerprint,
                checked.target_id,
                checked.target_policy_fingerprint,
            ):
                await run.stop("DATABASE_FINGERPRINT_MISMATCH")
            run.set(
                dry_run_plan=prediction, transaction_outcome=TransactionOutcome.DRY_RUN
            )
            if not prediction.ready:
                await run.stop(
                    prediction.blockers[0]
                    if prediction.blockers
                    else "SDK_LOAD_REVIEW_REQUIRED"
                )
        else:
            assert binding.loader is not None and load_request is not None
            try:
                result = await binding.loader.execute(load_request)
            except BaseException as error:
                if not isinstance(error, (Exception, asyncio.CancelledError)):
                    raise
                try:
                    async with asyncio.timeout(run.dependencies.cleanup_seconds):
                        if binding.staging and context:
                            staged = await binding.staging.get_run(context)
                            if staged.status in {
                                StagingRunStatus.ROLLED_BACK,
                                StagingRunStatus.CANCELLED,
                            }:
                                run.set(
                                    transaction_outcome=TransactionOutcome.ROLLED_BACK
                                )
                            elif staged.status is StagingRunStatus.COMMITTED:
                                # Staging подтверждает commit, но не заменяет
                                # потерянный result и его фактические counts.
                                run.set(
                                    transaction_outcome=TransactionOutcome.COMMITTED
                                )
                            elif staged.status in {
                                StagingRunStatus.EXECUTING,
                                StagingRunStatus.UNKNOWN,
                            }:
                                run.set(transaction_outcome=TransactionOutcome.UNKNOWN)
                except BaseException as cleanup_error:
                    if not isinstance(
                        cleanup_error, (Exception, asyncio.CancelledError)
                    ):
                        raise
                    run.set(
                        warnings=(
                            *run.result.warnings,
                            "SDK_STAGING_OUTCOME_UNAVAILABLE",
                        ),
                        transaction_outcome=TransactionOutcome.UNKNOWN,
                    )
                raise
            if (
                result.normalized_fingerprint,
                result.mapping_fingerprint,
                result.database_fingerprint,
                result.target_id,
            ) != (
                checked.normalized_fingerprint,
                checked.plan.fingerprint,
                checked.database_fingerprint,
                checked.target_id,
            ):
                from structuraguard.exceptions import LoadError

                run.set(transaction_outcome=TransactionOutcome.UNKNOWN)
                raise LoadError(
                    error_code="LOAD_OUTCOME_UNKNOWN", message="LOAD_OUTCOME_UNKNOWN"
                )
            run.set(
                load_result=result,
                transaction_outcome=TransactionOutcome.COMMITTED,
                warnings=(*run.result.warnings, *result.warnings),
                audit_references=(
                    *run.result.audit_references,
                    *((result.audit_head,) if result.audit_head else ()),
                ),
            )
        run.set(load_report=report(data, checked))

    await run.perform(S.LOADING, execute, transactional=not run.result.dry_run)
