"""Детерминированные artifacts и часы staging contract suite."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.staging import (
    StagingArtifactReference,
    StagingRetentionPolicy,
    StagingRunSpec,
)
from tests.fakes.provenance import provenance_fixture


@dataclass
class StagingClock:
    now: datetime = datetime(2026, 9, 13, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True)
class StagingCase:
    spec: StagingRunSpec
    batches: tuple[NormalizedBatch, ...]


async def staging_case(
    clock: StagingClock, policy: StagingRetentionPolicy
) -> StagingCase:
    fixture = await provenance_fixture()
    manifest = fixture.normalized[-1].manifest
    assert manifest is not None
    fingerprint = "sha256:" + "a" * 64
    context = StagingContext(
        run_id="run-1",
        staging_id="staging-1",
        target_id="main",
        database_fingerprint=fingerprint,
        target_policy_fingerprint=fingerprint,
        normalized_fingerprint=manifest.normalized_fingerprint,
        expires_at=clock.now + timedelta(hours=1),
        max_records=100,
    )
    return StagingCase(
        StagingRunSpec(
            context=context,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            parse_plan_fingerprint=manifest.parse_plan_fingerprint,
            mapping_plan_fingerprint=fingerprint,
            batch_count=len(fixture.normalized),
            record_count=manifest.record_count,
            references=tuple(
                StagingArtifactReference(
                    kind=kind,
                    artifact_id=f"artifact-{kind}",
                    fingerprint=(
                        manifest.normalized_fingerprint
                        if kind == "normalized"
                        else fingerprint
                    ),
                    retained_until=clock.now + timedelta(days=90),
                )
                for kind in policy.retain_kinds
            ),
        ),
        fixture.normalized,
    )
