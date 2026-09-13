"""M15 gates проверяются до reader/writer и на полном parser stream."""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import replace
from uuid import UUID

import pytest
from tests.fakes.pipeline import FakeDatabase, FakeParser, defaults, engine, request

from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.constraint_validation import (
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.mapping import MappingPlan
from structuraguard.contracts.normalization import NormalizerSpec
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.provenance import NormalizationBinding, ProvenancePolicy
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.contracts.source import ExtractedBatch, SourceArtifact
from structuraguard.ports.source import ParseContext
from structuraguard.security.audit import AuditChain, HMACAuditSigner, MemoryAuditKeys
from structuraguard.security.audit_store import MemoryAuditChainStore
from structuraguard.security.session import SecuritySession


class ObservedDatabase(FakeDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        self.reads += 1
        return await super().read(request, catalog=catalog)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field",
    ("normalized_fingerprint", "database_fingerprint", "target_policy_fingerprint"),
)
async def test_validly_hashed_foreign_mapping_rejected_before_key_reads(
    field: str,
) -> None:
    db = ObservedDatabase()
    sdk = engine(db)
    source = await sdk.inspect_source(request())
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan
        data = await sdk.parse_semantically(source, plan=plan)
        proposal = await sdk.create_mapping_plan(data)
        assert proposal.plan
        wire = proposal.plan.model_dump(mode="python", exclude={"fingerprint"})
        wire[field] = canonical_sha256_value(("foreign", field))
        assert wire[field] != getattr(proposal.plan, field)
        foreign = MappingPlan.model_validate(wire)
        result = await sdk.execute(data, plan=foreign)
        assert result.status is S.NEEDS_REVIEW
        assert result.mapping_validation and result.errors
        assert result.errors[-1].stage is S.MAPPING_PLAN_VALIDATING
        assert db.reads == db.writes == 0 and not db.artifacts


class TruncatedParser(FakeParser):
    async def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        stream = super().parse(source, context)
        try:
            async for batch in stream:
                yield batch.model_copy(update={"is_last": False, "manifest": None})
        finally:
            assert isinstance(stream, AsyncGenerator)
            await stream.aclose()


@pytest.mark.anyio
async def test_parser_without_terminal_manifest_cannot_reach_semantics_or_db() -> None:
    db, parser = ObservedDatabase(), TruncatedParser()
    result = await engine(db, parser=parser).ingest(request())
    assert result.status in {S.FAILED, S.REJECTED_SECURITY}
    assert result.errors[-1].stage is S.TECHNICAL_PARSING
    assert result.parse_plan is None and result.extraction is None
    assert parser.closed
    assert db.inspector.calls == db.reads == db.writes == 0


@pytest.mark.anyio
async def test_required_json_schema_failure_never_stages() -> None:
    db = ObservedDatabase()
    result = await engine(
        dependencies=replace(defaults(db), json_schema="false")
    ).ingest(request())
    assert result.status is S.NEEDS_REVIEW
    assert result.errors[-1].code == "JSON_SCHEMA_VALIDATION_FAILED"
    assert result.validation_report and result.validation_report.issues
    assert db.writes == 0 and not db.artifacts


@pytest.mark.anyio
async def test_value_changing_normalization_is_explicitly_blocked() -> None:
    db = ObservedDatabase()
    # Поле берётся из реального ParsePlan, policy задаётся до второго run.
    baseline = await engine(db).analyze(request())
    assert baseline.parse_plan
    field = baseline.parse_plan.fields[0]
    entity = baseline.parse_plan.entities[0]
    policy = ProvenancePolicy(
        normalizations=(
            NormalizationBinding(
                field=SemanticFieldRef(
                    entity_type=entity.entity_type, field_name=field.semantic_name
                ),
                steps=(NormalizerSpec(normalizer_id="trim"),),
            ),
        )
    )
    result = await engine(dependencies=replace(defaults(db), provenance=policy)).ingest(
        request(b'[{"name":" Ada ","city":"Riga"},{"name":" Bob ","city":"Oslo"}]')
    )
    assert result.status is S.NEEDS_REVIEW
    assert result.errors[-1].code == "DRY_RUN_PROVENANCE_UNVERIFIED"
    assert result.errors[-1].stage is S.NORMALIZING
    assert db.reads == db.writes == 0 and not db.artifacts


@pytest.mark.anyio
async def test_strict_risky_format_requires_sandbox_before_parser_execution() -> None:
    db, parser = FakeDatabase(), FakeParser()
    deps = replace(
        defaults(db),
        security=SecurityPolicy(
            allowed_formats=("json",),
            parser_trust="trusted",
            strict_mode=True,
            risky_formats=("json",),
        ),
    )
    result = await engine(dependencies=deps, parser=parser).ingest(request())
    assert result.status is S.REJECTED_SECURITY
    assert result.errors[-1].code == "SECURITY_SANDBOX_REQUIRED"
    assert parser.probes == 1 and parser.parses == 0
    assert db.inspector.calls == db.writes == 0


@pytest.mark.anyio
async def test_missing_audit_key_fails_before_source_probe() -> None:
    parser = FakeParser()
    key = UUID("aaaaaaaa-bbbb-4ccc-addd-eeeeeeeeeeee")

    def audit(resources: SecuritySession) -> AuditChain:
        return AuditChain(
            chain_id=key,
            run_id=resources.run_id,
            key_id=key,
            policy_fingerprint=resources.policy.fingerprint,
            signer=HMACAuditSigner(MemoryAuditKeys({})),
            store=MemoryAuditChainStore(),
        )

    result = await engine(
        dependencies=replace(defaults(), audit=audit, actor_id=key), parser=parser
    ).ingest(request())
    assert result.status in {S.FAILED, S.REJECTED_SECURITY}
    assert result.errors and not result.audit_references
    assert parser.probes == parser.parses == 0
    assert result.source_report is None
