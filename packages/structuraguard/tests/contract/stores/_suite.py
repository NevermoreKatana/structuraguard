"""Один проверяемый lifecycle для memory и PostgreSQL implementations."""

from datetime import timedelta

import pytest
from tests.fakes.staging import StagingCase, StagingClock

from structuraguard.contracts.staging import StagingRunStatus
from structuraguard.exceptions import StagingError
from structuraguard.ports.stores import RunStagingStore, StagingStore


class StagingContract:
    @pytest.mark.parametrize(
        "status",
        [
            StagingRunStatus.COMMITTED,
            StagingRunStatus.QUARANTINED,
            StagingRunStatus.ROLLED_BACK,
            StagingRunStatus.CANCELLED,
        ],
    )
    async def test_closed_execution_retains_references_and_cannot_be_reopened(
        self, store: RunStagingStore, case: StagingCase, status: StagingRunStatus
    ) -> None:
        ctx = case.spec.context
        await store.begin(case.spec)
        for batch in case.batches:
            await store.stage(batch, ctx)
        run = await store.get_run(ctx)
        run = await store.seal(ctx, expected_revision=run.revision)
        run = await store.transition(
            ctx, expected_revision=run.revision, status=StagingRunStatus.EXECUTING
        )
        run = await store.transition(ctx, expected_revision=run.revision, status=status)
        assert run.status is status
        assert run.spec.references == case.spec.references
        assert all(
            r.references == case.spec.references
            for r in await store.read_records(ctx, offset=0, limit=10)
        )
        with pytest.raises(StagingError, match="STAGING_STATE_INVALID"):
            await store.transition(
                ctx, expected_revision=run.revision, status=StagingRunStatus.EXECUTING
            )
        assert await store.get_run(ctx) == run

    async def test_stage_seal_read_and_idempotent_batch(
        self, store: RunStagingStore, case: StagingCase
    ) -> None:
        assert isinstance(store, StagingStore)
        assert isinstance(store, RunStagingStore)
        ctx = case.spec.context
        opened = await store.begin(case.spec)
        assert opened.status is StagingRunStatus.OPEN
        assert await store.begin(case.spec) == opened
        for batch in case.batches:
            await store.stage(batch, ctx)
            before = await store.get_run(ctx)
            await store.stage(batch, ctx)
            assert await store.get_run(ctx) == before
        run = await store.get_run(ctx)
        batches = await store.read_batches(ctx, offset=0, limit=10)
        assert tuple(b.summary for b in batches) == tuple(
            b.to_summary() for b in case.batches
        )
        receipt = await store.seal(ctx, expected_revision=run.revision)
        assert receipt.status is StagingRunStatus.SEALED
        assert receipt.record_count == case.spec.record_count
        page = await store.read_records(ctx, offset=0, limit=1)
        assert len(page) == 1
        assert page[0].references == case.spec.references
        assert page[0].record_id == case.batches[0].records[0].record_id
        assert "Ada" not in page[0].model_dump_json()
        assert "artifact" not in repr(page[0])
        assert await store.read_records(ctx, offset=10, limit=1) == ()
        with pytest.raises(StagingError, match="STAGING_STATE_INVALID"):
            await store.stage(case.batches[0], ctx)

    async def test_missing_run_never_bootstraps(
        self, store: RunStagingStore, case: StagingCase
    ) -> None:
        with pytest.raises(StagingError, match="STAGING_RUN_NOT_FOUND"):
            await store.stage(case.batches[0], case.spec.context)

    async def test_context_mismatch_and_stale_revision(
        self, store: RunStagingStore, case: StagingCase
    ) -> None:
        ctx = case.spec.context
        await store.begin(case.spec)
        wrong = ctx.model_copy(update={"normalized_fingerprint": "sha256:" + "f" * 64})
        with pytest.raises(StagingError, match="STAGING_CONTEXT_MISMATCH"):
            await store.get_run(wrong)
        for batch in case.batches:
            await store.stage(batch, ctx)
        with pytest.raises(StagingError, match="STAGING_REVISION_CONFLICT"):
            await store.seal(ctx, expected_revision=0)

    async def test_incomplete_seal_has_no_effect(
        self, store: RunStagingStore, case: StagingCase
    ) -> None:
        before = await store.begin(case.spec)
        with pytest.raises(StagingError, match="STAGING_INCOMPLETE"):
            await store.seal(case.spec.context, expected_revision=before.revision)
        assert await store.get_run(case.spec.context) == before

    async def test_expiry_denies_append_but_allows_terminal_cleanup(
        self, store: RunStagingStore, case: StagingCase, clock: StagingClock
    ) -> None:
        run = await store.begin(case.spec)
        clock.now = case.spec.context.expires_at
        with pytest.raises(StagingError, match="STAGING_EXPIRED"):
            await store.stage(case.batches[0], case.spec.context)
        closed = await store.transition(
            case.spec.context,
            expected_revision=run.revision,
            status=StagingRunStatus.CANCELLED,
        )
        assert closed.status is StagingRunStatus.CANCELLED

    async def test_cleanup_retains_tombstone_and_does_not_delete_early(
        self,
        store: RunStagingStore,
        cleaner: RunStagingStore,
        case: StagingCase,
        clock: StagingClock,
    ) -> None:
        ctx = case.spec.context
        await store.begin(case.spec)
        for batch in case.batches:
            await store.stage(batch, ctx)
        run = await store.get_run(ctx)
        closed = await store.transition(
            ctx, expected_revision=run.revision, status=StagingRunStatus.FAILED
        )
        assert not (await cleaner.cleanup(ctx)).purged
        assert closed.cleanup_after is not None
        clock.now = closed.cleanup_after
        purged = await cleaner.cleanup(ctx)
        assert purged.purged and purged.record_count == case.spec.record_count
        assert purged.spec.references == ()
        assert (await cleaner.cleanup(ctx)) == purged
        with pytest.raises(StagingError, match="STAGING_PURGED"):
            await store.read_records(ctx, offset=0, limit=10)
        with pytest.raises(StagingError, match="STAGING_RUN_EXISTS"):
            await store.begin(case.spec)

    async def test_unknown_never_expires_into_cleanup(
        self,
        store: RunStagingStore,
        cleaner: RunStagingStore,
        case: StagingCase,
        clock: StagingClock,
    ) -> None:
        ctx = case.spec.context
        await store.begin(case.spec)
        for batch in case.batches:
            await store.stage(batch, ctx)
        run = await store.get_run(ctx)
        run = await store.seal(ctx, expected_revision=run.revision)
        for status in (StagingRunStatus.EXECUTING, StagingRunStatus.UNKNOWN):
            run = await store.transition(
                ctx, expected_revision=run.revision, status=status
            )
        clock.now += timedelta(days=365)
        assert not (await cleaner.cleanup(ctx)).purged

    async def test_rejected_intake_does_not_change_run(
        self, store: RunStagingStore, case: StagingCase
    ) -> None:
        before = await store.begin(case.spec)
        bad = case.batches[0].model_copy(
            update={"batch_fingerprint": "sha256:" + "f" * 64}
        )
        with pytest.raises(StagingError, match="STAGING_INPUT_INVALID"):
            await store.stage(bad, case.spec.context)
        assert await store.get_run(case.spec.context) == before

    async def test_abandoned_preparation_expires_without_claiming_rollback(
        self,
        store: RunStagingStore,
        cleaner: RunStagingStore,
        case: StagingCase,
        clock: StagingClock,
    ) -> None:
        ctx = case.spec.context
        await store.begin(case.spec)
        await store.stage(case.batches[0], ctx)
        clock.now = ctx.expires_at
        expired = await cleaner.cleanup(ctx)
        assert expired.status is StagingRunStatus.EXPIRED
        assert not expired.purged and expired.cleanup_after is not None
        clock.now = expired.cleanup_after
        assert (await cleaner.cleanup(ctx)).purged
