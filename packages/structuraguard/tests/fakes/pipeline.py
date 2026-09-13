"""Fake внешних границ M15; parsing/validation внутри остаются настоящими."""

from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import SemanticParsingMode
from structuraguard.contracts.constraint_validation import (
    ConstraintMatch,
    ConstraintReadPolicy,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    IndexCatalog,
    IndexKeyCatalog,
    LoadContext,
    TableInspectionMetadata,
)
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingWeights,
)
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
    ExecutionStep,
    LoadRequest,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.mapping import (
    MappingPlanValidationResult,
    ValidatedMappingPlan,
)
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.reports import LoadReport
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.contracts.staging import (
    StagingArtifactKind,
    StagingArtifactReference,
    StagingRetentionPolicy,
    StagingRunStatus,
)
from structuraguard.exceptions import LoadError
from structuraguard.loading.projection import prepare
from structuraguard.mapping import MappingPlanValidator
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import JsonDocumentParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import DatabaseBinding, SDKDependencies, SourceRequest
from structuraguard.pipeline.composition import utc_now
from structuraguard.ports.parser import Parser
from structuraguard.ports.source import ParseContext, ProbeContext
from structuraguard.security.session import SecuritySession
from structuraguard.stores import MemoryStagingStore
from tests.fakes.mapping import catalog, column, scope_for, table

DATA = b'[{"name":"Ada","city":"Riga"},{"name":"Bob","city":"Oslo"}]'


class FakeParser:
    """Spy внешнего parser port; физические DTO создаёт настоящий JSON adapter."""

    adapter_id = "builtin.json"
    version = "1.0.0"
    priority = JsonDocumentParser().priority

    def __init__(self) -> None:
        self.probes = self.parses = 0
        self.closed = False
        self.delegate = JsonDocumentParser()

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        self.probes += 1
        return await self.delegate.probe(source, context)

    async def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        self.parses += 1
        stream = self.delegate.parse(source, context)
        try:
            async for batch in stream:
                yield batch
        finally:
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()
            self.closed = True


class Stream:
    def __init__(self, data: bytes = DATA) -> None:
        self.data, self.offset = data, 0

    async def read(self, size: int) -> bytes:
        result = self.data[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def request(data: bytes = DATA) -> SourceRequest:
    return SourceRequest(stream=Stream(data), display_name="input.json")


def ranking() -> DeterministicMappingOptions:
    return DeterministicMappingOptions(
        weights=MappingWeights(
            name_similarity=Decimal(1),
            alias_match=Decimal(0),
            type_compatibility=Decimal(0),
            value_pattern_match=Decimal(0),
            structural_context=Decimal(0),
            database_relation_score=Decimal(0),
        )
    )


class Inspector:
    def __init__(self, catalog: DatabaseCatalog) -> None:
        self.catalog = catalog
        self.calls = 0

    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        self.calls += 1
        return self.catalog

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: ValidatedMappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        raise AssertionError("Inspector не должен писать")


class FakeDatabase:
    def __init__(self) -> None:
        key = column("field_0").model_copy(
            update={"nullable": False, "primary_key": True}
        )
        self.catalog = catalog(
            table(
                "records", column("field_0"), column("field_1", position=1)
            ).model_copy(
                update={
                    "primary_key": ("field_0",),
                    "inspection": TableInspectionMetadata(
                        indexes=(
                            IndexCatalog(
                                index_id="pk",
                                name="pk",
                                keys=(
                                    IndexKeyCatalog(column_id="field_0", collation="C"),
                                ),
                                unique=True,
                                origin="primary_key",
                            ),
                        )
                    ),
                    "columns": (key, column("field_1", position=1)),
                }
            )
        )
        self.inspector = Inspector(self.catalog)
        self.store = MemoryStagingStore(
            target_id=self.catalog.target_id, retention=StagingRetentionPolicy()
        )
        self.policy = DryRunPolicy(
            writer_principal="writer",
            mapping_policy=MappingValidationPolicy(
                policy_id="test",
                scope=scope_for(self.catalog),
                allow_schemas=("public",),
                allow_tables=(("public", "records"),),
                source_identity_allow=(scope_for(self.catalog).allow[0],),
            ),
            read_policy=ConstraintReadPolicy(
                allow_columns=scope_for(self.catalog).allow
            ),
        )
        self.plans = self.writes = 0
        self.error: str | None = None
        self.artifacts: list[DryRunRequest] = []

    def bind(self, resources: SecuritySession) -> DatabaseBinding:
        return DatabaseBinding(
            inspector=self.inspector,
            request=DatabaseInspectionRequest(
                target_id=self.catalog.target_id,
                target_policy_fingerprint=self.catalog.target_policy_fingerprint,
            ),
            policy=self.policy,
            planner=self,
            reader=self,
            loader=self,
            staging=self.store,
            references=self.references,
        )

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        matches = tuple(
            ConstraintMatch(lookup_id=key.lookup_id, exists=False, conflicts=False)
            for key in request.lookups
        )
        return ConstraintReadResult(
            request_fingerprint=request.fingerprint,
            snapshot_fingerprint=canonical_sha256_value(
                (request.fingerprint, tuple(m.canonical_json() for m in matches))
            ),
            matches=matches,
        )

    async def references(
        self, snapshot: DryRunRequest, run_id: str, deadline: datetime
    ) -> tuple[StagingArtifactReference, ...]:
        self.artifacts.append(snapshot)
        plan = snapshot.mapping
        return tuple(
            StagingArtifactReference(
                kind=kind,
                artifact_id=f"test-{kind}-{run_id}",
                fingerprint=plan.normalized_fingerprint
                if kind is StagingArtifactKind.NORMALIZED
                else plan.fingerprint
                if kind is StagingArtifactKind.MAPPING
                else plan.source_fingerprint,
                retained_until=deadline + timedelta(days=90),
            )
            for kind in StagingArtifactKind
        )

    async def plan(self, request: DryRunRequest) -> DryRunExecutionPlan:
        self.plans += 1
        prepared = await prepare(request)
        checked = await MappingPlanValidator(
            policy=self.policy.mapping_policy
        ).validate(
            request.mapping, prepared.manifest, self.catalog, profile=prepared.profile
        )
        assert isinstance(checked, MappingPlanValidationResult)
        assert isinstance(checked.validated_plan, ValidatedMappingPlan)
        fp = request.mapping.fingerprint
        return DryRunExecutionPlan(
            target_id=self.catalog.target_id,
            database_fingerprint=self.catalog.database_fingerprint,
            target_policy_fingerprint=self.catalog.target_policy_fingerprint,
            normalized_fingerprint=prepared.manifest.normalized_fingerprint,
            mapping_fingerprint=fp,
            mapping_validation_fingerprint=checked.validated_plan.validation_fingerprint,
            projection_fingerprint=canonical_sha256_value(prepared.data),
            policy_fingerprint=canonical_sha256_value(self.policy),
            validation_fingerprint=fp,
            read_snapshot_fingerprint=fp,
            table_order=("public.records",),
            steps=tuple(
                ExecutionStep(
                    unit_id=row.record_id,
                    record_id=prepared.origins[row.record_id].record_id,
                    entity_id=prepared.origins[row.record_id].entity_id,
                    table_id=row.collection_id,
                    value_ids=prepared.origins[row.record_id].value_ids,
                    column_ids=tuple(c.field_id for c in row.values),
                    identity_column_ids=(),
                    update_column_ids=(),
                    dependencies=(),
                    action="insert",
                )
                for row in prepared.data.records
            ),
        )

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        run = await self.store.get_run(request.staging_context)
        run = await self.store.transition(
            request.staging_context,
            expected_revision=run.revision,
            status=StagingRunStatus.EXECUTING,
        )
        if self.error:
            await self.store.transition(
                request.staging_context,
                expected_revision=run.revision,
                status=StagingRunStatus.ROLLED_BACK,
            )
            raise LoadError(error_code=self.error, message="synthetic failure")
        self.writes += 1
        run = await self.store.transition(
            request.staging_context,
            expected_revision=run.revision,
            status=StagingRunStatus.COMMITTED,
        )
        plan = request.snapshot.mapping
        manifest = request.snapshot.batches[-1].manifest
        assert manifest is not None and run.sealed_fingerprint is not None
        return PostgreSQLLoadResult(
            run_id=request.staging_context.run_id,
            target_id=plan.target_id,
            database_fingerprint=plan.database_fingerprint,
            normalized_fingerprint=plan.normalized_fingerprint,
            mapping_fingerprint=plan.fingerprint,
            execution_plan_fingerprint=plan.fingerprint,
            sealed_fingerprint=run.sealed_fingerprint,
            policy_fingerprint=canonical_sha256_value(self.policy),
            inserted=manifest.record_count,
            updated=0,
            skipped=0,
            loaded_records=manifest.record_count,
            rejected_records=0,
            generated_at=utc_now(),
        )


def defaults(database: FakeDatabase | None = None) -> SDKDependencies:
    return SDKDependencies(
        security=SecurityPolicy(allowed_formats=("json",), parser_trust="trusted"),
        parsing=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        ranking=ranking(),
        database=database.bind if database else None,
    )


def engine(
    database: FakeDatabase | None = None,
    *,
    dependencies: SDKDependencies | None = None,
    parser: Parser | None = None,
) -> AsyncStructuraGuard:
    registry = ParserRegistry()
    registry.register(parser or FakeParser())
    return AsyncStructuraGuard(
        parser_registry=registry, dependencies=dependencies or defaults(database)
    )
