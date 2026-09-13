"""Retention, forged DTO, bounded intake и запрет произвольной bootstrap schema."""

from datetime import timedelta

import pytest
from pydantic import SecretStr, ValidationError
from tests.fakes.provenance import rehash
from tests.fakes.staging import StagingClock, staging_case

from structuraguard.contracts.staging import (
    StagingArtifactKind,
    StagingLimits,
    StagingRetentionPolicy,
    StagingRunStatus,
)
from structuraguard.exceptions import StagingError
from structuraguard.stores import (
    MemoryStagingStore,
    PostgreSQLStagingStore,
    PostgreSQLStagingTarget,
)

pytestmark = pytest.mark.anyio


async def test_retention_is_trusted_and_does_not_persist_forbidden_raw_reference() -> (
    None
):
    clock = StagingClock()
    broad = StagingRetentionPolicy()
    narrow = StagingRetentionPolicy(retain_kinds=(StagingArtifactKind.NORMALIZED,))
    store = MemoryStagingStore(target_id="main", retention=narrow, clock=clock)
    case = await staging_case(clock, broad)
    with pytest.raises(StagingError, match="STAGING_RETENTION_MISMATCH"):
        await store.begin(case.spec)
    with pytest.raises(StagingError, match="STAGING_RUN_NOT_FOUND"):
        await store.get_run(case.spec.context)
    case = await staging_case(clock, narrow)
    await store.begin(case.spec)
    for batch in case.batches:
        await store.stage(batch, case.spec.context)
    run = await store.get_run(case.spec.context)
    await store.seal(case.spec.context, expected_revision=run.revision)
    records = await store.read_records(case.spec.context, offset=0, limit=10)
    assert all(
        tuple(r.kind for r in item.references) == (StagingArtifactKind.NORMALIZED,)
        for item in records
    )
    assert all("Ada" not in item.model_dump_json() for item in records)


async def test_reference_lease_must_cover_run_and_terminal_retention() -> None:
    clock = StagingClock()
    policy = StagingRetentionPolicy()
    case = await staging_case(clock, policy)
    refs = tuple(
        r.model_copy(update={"retained_until": case.spec.context.expires_at})
        for r in case.spec.references
    )
    store = MemoryStagingStore(target_id="main", retention=policy, clock=clock)
    with pytest.raises(StagingError, match="STAGING_REFERENCE_EXPIRES"):
        await store.begin(case.spec.model_copy(update={"references": refs}))


async def test_forged_huge_intake_is_bounded_before_serialization() -> None:
    clock = StagingClock()
    policy = StagingRetentionPolicy()
    case = await staging_case(clock, policy)
    store = MemoryStagingStore(target_id="main", retention=policy, clock=clock)
    before = await store.begin(case.spec)
    bad = case.batches[0].model_copy(update={"producer": "private-canary" * 1_000_000})
    with pytest.raises(StagingError, match="STAGING_LIMIT_EXCEEDED") as error:
        await store.stage(bad, case.spec.context)
    assert "private-canary" not in str(error.value)
    assert await store.get_run(case.spec.context) == before


async def test_same_batch_ordinal_with_valid_different_content_is_rejected() -> None:
    clock = StagingClock()
    policy = StagingRetentionPolicy()
    case = await staging_case(clock, policy)
    store = MemoryStagingStore(target_id="main", retention=policy, clock=clock)
    await store.begin(case.spec)
    await store.stage(case.batches[0], case.spec.context)
    batch = case.batches[0]
    changed = batch.model_copy(
        update={
            "records": (
                batch.records[0].model_copy(update={"record_id": "different-record"}),
            )
        }
    )
    changed = rehash((changed, *case.batches[1:]))[0]
    with pytest.raises(StagingError, match="STAGING_CONTENT_MISMATCH"):
        await store.stage(changed, case.spec.context)


async def test_success_cleanup_and_terminal_state_cannot_be_reopened() -> None:
    clock = StagingClock()
    policy = StagingRetentionPolicy(success_seconds=1, failure_seconds=10)
    case = await staging_case(clock, policy)
    store = MemoryStagingStore(target_id="main", retention=policy, clock=clock)
    ctx = case.spec.context
    await store.begin(case.spec)
    for batch in case.batches:
        await store.stage(batch, ctx)
    run = await store.get_run(ctx)
    run = await store.seal(ctx, expected_revision=run.revision)
    for status in (StagingRunStatus.EXECUTING, StagingRunStatus.COMMITTED):
        run = await store.transition(ctx, expected_revision=run.revision, status=status)
    assert run.cleanup_after == clock.now + timedelta(seconds=1)
    with pytest.raises(StagingError, match="STAGING_STATE_INVALID"):
        await store.transition(
            ctx, expected_revision=run.revision, status=StagingRunStatus.OPEN
        )
    clock.now += timedelta(seconds=1)
    assert (await store.cleanup(ctx)).purged


@pytest.mark.parametrize(
    "schema",
    ["public", "app", "pg_catalog", "sg_staging_a;DROP SCHEMA app", 'sg_staging_a"'],
)
async def test_only_reserved_staging_schema_is_accepted(schema: str) -> None:
    with pytest.raises(ValidationError):
        PostgreSQLStagingTarget(
            dsn=SecretStr("postgresql+asyncpg://writer:pass@localhost/db"),
            principal="writer",
            inspector_principal="inspector",
            target_id="main",
            namespace="app",
            schema_name=schema,
        )


async def test_writer_config_rejects_forged_bootstrap_and_inspector_principal() -> None:
    config = PostgreSQLStagingTarget(
        dsn=SecretStr("postgresql+asyncpg://writer:pass@localhost/db"),
        principal="writer",
        inspector_principal="inspector",
        target_id="main",
        namespace="app",
    )
    policy = StagingRetentionPolicy()
    for update in ({"schema_name": "public"}, {"principal": "inspector"}):
        with pytest.raises(StagingError, match="STAGING_TARGET_INVALID"):
            PostgreSQLStagingStore(config.model_copy(update=update), retention=policy)
    with pytest.raises(StagingError, match="STAGING_PRINCIPAL_FORBIDDEN"):
        PostgreSQLStagingStore(
            config.model_copy(update={"purpose": "bootstrap"}), retention=policy
        )
    assert "pass" not in repr(config) and "pass" not in config.model_dump_json()


async def test_batch_and_read_limits_fail_without_partial_mutation() -> None:
    clock = StagingClock()
    policy = StagingRetentionPolicy()
    case = await staging_case(clock, policy)
    store = MemoryStagingStore(
        target_id="main",
        retention=policy,
        limits=StagingLimits(max_records=1),
        clock=clock,
    )
    with pytest.raises(StagingError, match="STAGING_LIMIT_EXCEEDED"):
        await store.begin(case.spec)
    with pytest.raises(StagingError, match="STAGING_LIMIT_EXCEEDED"):
        await store.read_records(case.spec.context, offset=-1, limit=100000)
