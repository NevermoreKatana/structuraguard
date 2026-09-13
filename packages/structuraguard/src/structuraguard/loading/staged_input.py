"""Связать supplied values с immutable sealed staging до открытия writer DB."""

import asyncio
from datetime import datetime

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.loading import LoadRequest
from structuraguard.contracts.staging import (
    StagedBatch,
    StagedRecord,
    StagingArtifactKind,
    StagingRun,
    StagingRunStatus,
)
from structuraguard.exceptions import StructuraGuardError
from structuraguard.ports.stores import RunStagingStore

from .projection import Prepared, checked, failure


async def verify_staging(
    store: RunStagingStore,
    request: LoadRequest,
    prepared: Prepared,
    *,
    deadline: datetime,
    max_bytes: int,
) -> StagingRun:
    run = checked(await store.get_run(request.staging_context), StagingRun, max_bytes)
    spec, context = run.spec, request.staging_context
    manifest, mapping = prepared.manifest, prepared.request.mapping
    if (
        run.status is not StagingRunStatus.SEALED
        or run.revision != request.staging_revision
        or run.purged
    ):
        raise failure("LOAD_STAGING_NOT_SEALED")
    if (
        spec.context != context
        or context.expires_at <= deadline
        or any(ref.retained_until <= deadline for ref in spec.references)
    ):
        raise failure("LOAD_STAGING_EXPIRED")
    if (
        (
            context.target_id,
            context.target_policy_fingerprint,
            context.database_fingerprint,
            context.normalized_fingerprint,
        )
        != (
            mapping.target_id,
            mapping.target_policy_fingerprint,
            mapping.database_fingerprint,
            manifest.normalized_fingerprint,
        )
        or (
            spec.source_fingerprint,
            spec.extraction_fingerprint,
            spec.parse_plan_fingerprint,
            spec.mapping_plan_fingerprint,
        )
        != (
            mapping.source_fingerprint,
            mapping.extraction_fingerprint,
            mapping.parse_plan_fingerprint,
            mapping.fingerprint,
        )
        or (run.batch_count, spec.batch_count) != (len(prepared.request.batches),) * 2
        or (run.record_count, spec.record_count) != (manifest.record_count,) * 2
        or run.spec_fingerprint != canonical_sha256_value(spec)
        or {ref.kind for ref in spec.references} != set(StagingArtifactKind)
    ):
        raise failure("LOAD_STAGING_BINDING_MISMATCH")
    batches = prepared.request.batches
    for offset in range(0, len(batches), 100):
        page = await store.read_batches(
            context, offset=offset, limit=min(100, len(batches) - offset)
        )
        if len(page) != min(100, len(batches) - offset):
            raise failure("LOAD_STAGING_INCOMPLETE")
        for i, item in enumerate(page, offset):
            item = checked(item, StagedBatch, max_bytes)
            if item.summary != batches[i].to_summary():
                raise failure("LOAD_STAGING_BINDING_MISMATCH")
    records = tuple(
        (b.batch_index, i, r) for b in batches for i, r in enumerate(b.records)
    )
    fingerprints = tuple(canonical_sha256_value(r) for _, _, r in records)
    for offset in range(0, len(records), 100):
        record_page = await store.read_records(
            context, offset=offset, limit=min(100, len(records) - offset)
        )
        if len(record_page) != min(100, len(records) - offset):
            raise failure("LOAD_STAGING_INCOMPLETE")
        for i, record_item in enumerate(record_page, offset):
            record_item = checked(record_item, StagedRecord, max_bytes)
            batch_index, index, record = records[i]
            if (
                record_item.record_id,
                record_item.record_index,
                record_item.batch_index,
                record_item.index_in_batch,
                record_item.record_fingerprint,
                record_item.references,
            ) != (
                record.record_id,
                i,
                batch_index,
                index,
                fingerprints[i],
                spec.references,
            ):
                raise failure("LOAD_STAGING_BINDING_MISMATCH")
    if await store.read_batches(
        context, offset=len(batches), limit=1
    ) or await store.read_records(context, offset=len(records), limit=1):
        raise failure("LOAD_STAGING_BINDING_MISMATCH")
    seal = canonical_sha256_value(
        (
            run.spec_fingerprint,
            tuple(b.to_summary().canonical_json() for b in batches),
            fingerprints,
        )
    )
    if run.sealed_fingerprint != seal:
        raise failure("LOAD_STAGING_BINDING_MISMATCH")
    return run


async def finish_staging(
    store: RunStagingStore, run: StagingRun, status: StagingRunStatus, seconds: float
) -> tuple[bool, bool]:
    """Bounded finalize не меняет уже известный target outcome при отмене caller."""

    async def finish() -> bool:
        try:
            async with asyncio.timeout(seconds):
                await store.transition(
                    run.spec.context, expected_revision=run.revision, status=status
                )
            return True
        except (
            StructuraGuardError,
            OSError,
            ValueError,
            TypeError,
            TimeoutError,
            RuntimeError,
        ):
            return False

    task = asyncio.create_task(finish())
    deadline = asyncio.get_running_loop().time() + seconds
    cancelled = False
    while True:
        try:
            done, _ = await asyncio.wait(
                (task,), timeout=max(0, deadline - asyncio.get_running_loop().time())
            )
            if not done:
                task.cancel()
                task.add_done_callback(_consume_finish)
                return False, cancelled
            return task.result(), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.cancelled():
                return False, cancelled


def _consume_finish(task: asyncio.Task[bool]) -> None:
    if not task.cancelled():
        task.exception()
