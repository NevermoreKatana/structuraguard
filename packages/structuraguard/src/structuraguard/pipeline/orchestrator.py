"""Typed orchestration существующих components; один gate для всех entry points."""

from structuraguard.contracts.common import LoadOperation, ValidationDecision
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.mapping import MappingPlan, MappingPlanValidationResult
from structuraguard.contracts.mapping_validation import MappingPlanInputReport
from structuraguard.contracts.orchestration import IngestResult
from structuraguard.contracts.parsing import ParsePlan, ParsePlanValidationResult
from structuraguard.contracts.profiling import (
    NormalizedDataProfile,
    NormalizedProfileContext,
)
from structuraguard.domain.database_fingerprint import verify_database_fingerprint
from structuraguard.exceptions import StructuraGuardError
from structuraguard.parsers import ParserRegistry
from structuraguard.profiling import NormalizedDataProfiler
from structuraguard.structure.hybrid import HybridAnalysis

from . import loading, mapping, parsing, validation
from .composition import SDKDependencies
from .mapping import MappingProposal
from .session import PipelineError, RunSession
from .source import NormalizedData, SourceAnalysis, SourceRequest
from .state import TERMINAL


class Orchestrator:
    """Facade owner; source leases принадлежат вызывающему коду, ingest закрывает свои."""

    def __init__(self, dependencies: SDKDependencies, registry: ParserRegistry) -> None:
        self.dependencies, self.registry = dependencies, registry

    async def analyze(self, source: SourceRequest) -> IngestResult:
        run = RunSession(self.dependencies, self, dry_run=True)
        try:
            physical = await parsing.inspect(run, self.registry, source)
            await self.analyze_structure(physical)
            plan = await self.create_parse_plan(physical)
            if plan is None:
                await run.stop("NEEDS_SEMANTIC_ANALYSIS")
            assert plan is not None
            normalized = await self.parse_semantically(physical, plan=plan)
            proposal = await self.create_mapping_plan(normalized)
            await run.terminal(S.COMPLETED if proposal.plan else S.NEEDS_REVIEW)
        except PipelineError:
            pass
        finally:
            await run.aclose()
        return run.snapshot()

    def run(self, source: SourceAnalysis) -> RunSession:
        if source._run.owner is not self:
            raise StructuraGuardError(
                error_code="SDK_SOURCE_OWNER_MISMATCH",
                message="SDK_SOURCE_OWNER_MISMATCH",
            )
        source._run.ensure_open()
        if source._run.result.status in TERMINAL:
            raise StructuraGuardError(
                error_code="SDK_INVALID_TRANSITION", message="SDK_INVALID_TRANSITION"
            )
        source.manifest.validate_batches(source.batches)
        if (
            source.manifest != source._run.result.extraction
            or source.artifact != source._run.result.source_report
            or source.probe != source._run.result.probe
        ):
            raise StructuraGuardError(
                error_code="SOURCE_FINGERPRINT_MISMATCH",
                message="SOURCE_FINGERPRINT_MISMATCH",
            )
        return source._run

    async def inspect_source(self, source: SourceRequest) -> SourceAnalysis:
        run = RunSession(self.dependencies, self, dry_run=True)
        try:
            return await parsing.inspect(run, self.registry, source)
        except BaseException:
            await run.aclose()
            raise

    async def analyze_structure(
        self, source: SourceAnalysis, *, saved_plan: ParsePlan | None = None
    ) -> HybridAnalysis:
        run = self.run(source)
        if run.analysis is None:
            run.analysis = await parsing.analyze(source, saved_plan=saved_plan)
        elif saved_plan is not None and run.analysis.plan != saved_plan:
            await run.stop("SOURCE_FINGERPRINT_MISMATCH")
        return run.analysis

    async def create_parse_plan(
        self, source: SourceAnalysis, *, structure: HybridAnalysis | None = None
    ) -> ParsePlan | None:
        run = self.run(source)
        analysis = await self.analyze_structure(source)
        if structure is not None and structure != analysis:
            await run.stop("SOURCE_FINGERPRINT_MISMATCH")

        if run.result.status in {S.PARSE_PLAN_CREATED, S.PARSE_PLAN_VALIDATING}:
            return run.result.parse_plan

        async def create() -> ParsePlan | None:
            if analysis.plan:
                run.set(
                    parse_plan=analysis.plan,
                    parse_plan_fingerprint=analysis.plan.fingerprint,
                )
            return analysis.plan

        return await run.perform(S.PARSE_PLAN_CREATED, create)

    async def validate_parse_plan(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> ParsePlanValidationResult:
        run = self.run(source)
        if run.analysis is None:
            await self.analyze_structure(source, saved_plan=plan)
        if run.result.status is S.STRUCTURE_ANALYZING:
            await self.create_parse_plan(source)
        return await parsing.validate(source, plan)

    async def parse_semantically(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> NormalizedData:
        run = self.run(source)
        if run.analysis is None:
            await self.analyze_structure(source, saved_plan=plan)
        if run.result.status is S.STRUCTURE_ANALYZING:
            await self.create_parse_plan(source)
        return await parsing.execute(source, plan)

    async def profile_records(self, source: NormalizedData) -> NormalizedDataProfile:
        run = self.run(source.source)
        source.manifest.validate_batches(source.batches)
        if (
            source.manifest.normalized_fingerprint != run.result.normalized_fingerprint
            or source.report != run.result.semantic_parse_report
            or source.plan.plan != run.result.parse_plan
        ):
            await run.stop("SOURCE_FINGERPRINT_MISMATCH")
        if run.result.normalized_profile:
            if (
                run.result.normalized_fingerprint
                != source.manifest.normalized_fingerprint
            ):
                await run.stop("SOURCE_FINGERPRINT_MISMATCH")
            return run.result.normalized_profile

        async def profile() -> NormalizedDataProfile:
            classification = (
                run.result.security_report.privacy[-1].classification
                if run.result.security_report.privacy
                else self.dependencies.privacy.baseline
            )
            result = await NormalizedDataProfiler().profile(
                source.replay(),
                context=NormalizedProfileContext(
                    data_classification=classification,
                    source_fingerprint=source.manifest.source.source_fingerprint,
                    extraction_fingerprint=source.manifest.extraction_fingerprint,
                    parse_plan_fingerprint=source.manifest.parse_plan_fingerprint,
                ),
            )
            run.set(normalized_profile=result)
            return result

        return await run.perform(S.NORMALIZED_DATA_PROFILING, profile)

    async def _inspect_database(self, run: RunSession) -> DatabaseCatalog:
        async def inspect() -> DatabaseCatalog:
            if self.dependencies.database is None:
                await run.stop("SDK_DATABASE_DEPENDENCY_REQUIRED")
            assert self.dependencies.database is not None
            if run.database is None:
                run.database = self.dependencies.database(run.resources)
            binding = run.database
            scope = binding.policy.mapping_policy.scope
            if (
                binding.request.target_id,
                binding.request.target_policy_fingerprint,
            ) != (scope.target_id, scope.target_policy_fingerprint):
                raise parsing.deny("DATABASE_TARGET_MISMATCH")
            catalog = await binding.inspector.inspect(binding.request)
            verify_database_fingerprint(catalog, catalog.database_fingerprint)
            if (catalog.target_id, catalog.target_policy_fingerprint) != (
                scope.target_id,
                scope.target_policy_fingerprint,
            ):
                raise parsing.deny("DATABASE_TARGET_MISMATCH")
            run.set(
                database_report=catalog,
                database_fingerprint=catalog.database_fingerprint,
            )
            return catalog

        return await run.perform(S.DATABASE_INSPECTING, inspect)

    async def inspect_database(
        self, *, source: NormalizedData | None = None
    ) -> DatabaseCatalog:
        if source is not None:
            run = self.run(source.source)
            await self.profile_records(source)
            return await self._inspect_database(run)
        run = RunSession(self.dependencies, self, dry_run=True)
        try:
            result = await self._inspect_database(run)
            await run.terminal(S.COMPLETED)
            return result
        finally:
            await run.aclose()

    async def create_mapping_plan(
        self,
        source: NormalizedData,
        *,
        database: DatabaseCatalog | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
    ) -> MappingProposal:
        run = self.run(source.source)
        profile = await self.profile_records(source)
        catalog = run.result.database_report or await self.inspect_database(
            source=source
        )
        if database is not None and database != catalog:
            await run.stop("DATABASE_FINGERPRINT_MISMATCH")
        return await mapping.create(source, catalog, profile, operation=operation)

    async def validate_mapping_plan(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        database: DatabaseCatalog | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        run = self.run(source.source)
        profile = await self.profile_records(source)
        catalog = run.result.database_report or await self.inspect_database(
            source=source
        )
        if database is not None and database != catalog:
            await run.stop("DATABASE_FINGERPRINT_MISMATCH")
        if run.result.status in {S.DATABASE_INSPECTING, S.MAPPING}:

            async def selected() -> None:
                run.set(mapping_plan=plan, mapping_plan_fingerprint=plan.fingerprint)

            await run.perform(S.MAPPING_PLAN_CREATED, selected)
        return await mapping.validate(source, catalog, plan, profile)

    async def execute(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        run = self.run(source.source)
        with run.execution(dry_run=dry_run):
            try:
                checked = await self.validate_mapping_plan(source, plan=plan)
                if (
                    not isinstance(checked, MappingPlanValidationResult)
                    or checked.validated_plan is None
                ):
                    await run.stop(
                        checked.issues[0].code
                        if checked.issues
                        else "MAPPING_PLAN_INVALID"
                    )
                assert (
                    isinstance(checked, MappingPlanValidationResult)
                    and checked.validated_plan is not None
                )
                _, report = await validation.validate(source, checked.validated_plan)
                if report.decision is not ValidationDecision.ACCEPTED:
                    await run.stop(
                        report.issues[0].code
                        if report.issues
                        else "SDK_VALIDATION_INCOMPLETE"
                    )
                await loading.load(
                    source, checked.validated_plan, idempotency_key=idempotency_key
                )
                await run.terminal(
                    S.COMPLETED_WITH_WARNINGS
                    if run.result.warnings
                    or (
                        run.result.load_report
                        and run.result.load_report.rejected_records
                    )
                    else S.COMPLETED
                )
            except PipelineError:
                pass
            return run.snapshot()

    async def ingest(
        self,
        source: SourceRequest,
        *,
        dry_run: bool = False,
        parse_plan: ParsePlan | None = None,
        mapping_plan: MappingPlan | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        run = RunSession(self.dependencies, self, dry_run=dry_run)
        try:
            physical = await parsing.inspect(run, self.registry, source)
            await self.analyze_structure(physical, saved_plan=parse_plan)
            plan = await self.create_parse_plan(physical)
            if plan is None:
                analysis = run.analysis
                await run.stop(
                    analysis.issues[0].code
                    if analysis and analysis.issues
                    else "NEEDS_SEMANTIC_ANALYSIS"
                )
            assert plan is not None
            normalized = await self.parse_semantically(physical, plan=plan)
            await self.profile_records(normalized)
            await self.inspect_database(source=normalized)
            if mapping_plan is None:
                proposal = await self.create_mapping_plan(
                    normalized, operation=operation
                )
                mapping_plan = proposal.plan
                if mapping_plan is None:
                    await run.stop("SDK_MAPPING_NEEDS_REVIEW")
            assert mapping_plan is not None
            await self.execute(
                normalized,
                plan=mapping_plan,
                dry_run=dry_run,
                idempotency_key=idempotency_key,
            )
        except PipelineError:
            pass
        finally:
            await run.aclose()

        return run.snapshot()
