"""Source security, technical extraction и отдельный semantic execution."""

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.injection import InjectionAction
from structuraguard.contracts.llm import LLMExecutionEnvironment, LLMRoutingMode
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParsePlan,
    ParsePlanValidationResult,
    StructureProfile,
)
from structuraguard.contracts.security import Resource
from structuraguard.contracts.semantic import LLMAnalysisContext
from structuraguard.contracts.source import ExtractedBatch, SourceArtifact
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.llm.run import LLMRunProvider
from structuraguard.mapping._inputs import bounded_size
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.execution import SelectedParser
from structuraguard.parsing import SemanticParsingSession
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.source import (
    BatchOptions,
    ParseContext,
    ProbeContext,
    SourceReader,
)
from structuraguard.security.scanner import InjectionAwareSecurityScanner
from structuraguard.security.signals import InjectionDetector
from structuraguard.structure import StructuralProfiler
from structuraguard.structure.hybrid import HybridAnalysis, HybridStructureAnalyzer

from .session import RunSession
from .source import (
    NormalizedData,
    SourceAnalysis,
    SourceRequest,
    classify_source,
    text_chunks,
)


def deny(code: str) -> SecurityPolicyError:
    return SecurityPolicyError(error_code=code, message=code)


async def inspect(
    run: RunSession, registry: ParserRegistry, request: SourceRequest
) -> SourceAnalysis:
    async with registry.session() as parsers:

        async def probe() -> tuple[SourceArtifact, SourceReader, SelectedParser]:
            if run.dependencies.security.parser_trust != "trusted":
                raise deny("SECURITY_SANDBOX_REQUIRED")
            if not run.dependencies.security.allowed_formats:
                raise deny("SECURITY_INPUT_REJECTED")
            snapshot = await run.resources.snapshot(
                request.stream, kind="stream", expected_size=request.expected_size
            )
            run.retain(snapshot.size_bytes)
            artifact = SourceArtifact(
                artifact_id="source-"
                + snapshot.source_fingerprint.removeprefix("sha256:"),
                display_name=request.display_name,
                media_type=request.media_type,
                size_bytes=snapshot.size_bytes,
                source_fingerprint=snapshot.source_fingerprint,
            )
            reader = run.resources.guarded_reader(snapshot, artifact)
            selected = await run.bounded(
                lambda: parsers.select(
                    artifact,
                    ProbeContext(
                        reader=reader,
                        source_fingerprint=artifact.source_fingerprint,
                        max_probe_bytes=min(65536, run.dependencies.max_snapshot_bytes),
                    ),
                ),
                resource=Resource.PARSER_TIME_MS,
            )
            detected = selected.probe_result.format_id
            policy = run.dependencies.security
            if detected not in policy.allowed_formats:
                raise deny("SECURITY_INPUT_REJECTED")
            if policy.strict_mode and detected in policy.risky_formats:
                raise deny("SECURITY_SANDBOX_REQUIRED")
            run.set(
                source_report=artifact,
                source_fingerprint=artifact.source_fingerprint,
                probe=selected.probe_result,
            )
            return artifact, reader, selected

        artifact, reader, selected = await run.perform(S.SOURCE_PROBING, probe)

        async def extract() -> SourceAnalysis:
            limits = run.dependencies.security.limits
            context = ParseContext(
                reader=reader,
                source_fingerprint=artifact.source_fingerprint,
                max_bytes=min(
                    limits.max_stream_bytes, run.dependencies.max_snapshot_bytes
                ),
                max_records=min(limits.max_records, run.dependencies.max_records),
                max_nesting_depth=limits.max_nesting_depth,
                batch_options=BatchOptions(
                    batch_size=min(500, run.dependencies.max_records),
                    max_batches=min(run.dependencies.max_batches, limits.max_chunks),
                ),
                detected_encoding=selected.probe_result.detected_encoding,
                max_text_chars=limits.max_text_chars,
                max_columns=limits.max_columns,
            )
            batches: list[ExtractedBatch] = []
            stream = selected.parse(artifact, context)
            try:
                async for batch in stream:
                    bounded_size(batch, run.dependencies.max_snapshot_bytes)
                    run.retain(len(batch.model_dump_json().encode()))
                    if len(batches) >= run.dependencies.max_batches:
                        raise deny("SECURITY_LIMIT_EXCEEDED")
                    run.resources.reserve(Resource.CHUNKS, 1)
                    if batch.record_count is not None:
                        run.resources.reserve(Resource.RECORDS, batch.record_count)
                    batches.append(batch)
            finally:
                await stream.aclose()
            if not batches or batches[-1].manifest is None:
                raise deny("PROVENANCE_INVALID")
            manifest = batches[-1].manifest
            manifest.validate_batches(batches)
            run.set(
                extraction=manifest,
                extraction_fingerprint=manifest.extraction_fingerprint,
            )
            return SourceAnalysis(
                artifact=artifact,
                probe=selected.probe_result,
                batches=tuple(batches),
                manifest=manifest,
                _run=run,
            )

        return await run.perform(
            S.TECHNICAL_PARSING,
            lambda: run.bounded(extract, resource=Resource.PARSER_TIME_MS),
        )


async def analyze(
    source: SourceAnalysis, *, saved_plan: ParsePlan | None = None
) -> HybridAnalysis:
    run = source._run

    async def profile() -> StructureProfile:
        result = await StructuralProfiler(
            options=run.dependencies.parsing.structural.execution.source_limits
        ).profile(source.replay())
        run.set(structure_profile=result)
        return result

    await run.perform(S.STRUCTURE_PROFILING, profile)

    async def semantic() -> HybridAnalysis:
        deps = run.dependencies
        privacy_reports = await classify_source(source)
        privacy = privacy_reports[-1]
        run.set(
            security_report=run.result.security_report.model_copy(
                update={"privacy": privacy_reports}
            )
        )
        routing = run.resources.llm_policy(deps.routing) if deps.routing else None
        routing_id = routing.policy_id if routing else "no_llm"
        routing_fp = (
            canonical_sha256_value(routing) if routing else deps.security.fingerprint
        )
        context = LLMAnalysisContext(
            run_id=run.run_id,
            data_classification=privacy.classification,
            routing_policy_id=routing_id,
            routing_policy_fingerprint=routing_fp,
            redaction_fingerprint=canonical_sha256_value(privacy),
        )
        if deps.scanner:
            run.scanner = InjectionAwareSecurityScanner(
                scanner=deps.scanner,
                policy=deps.injection,
                run_id=run.run_id,
                routing_policy_id=routing_id,
                routing_policy_fingerprint=routing_fp,
                clock=deps.clock,
            )
        detector = InjectionDetector(deps.injection)
        summaries = []
        texts: list[str] = []
        size = 0
        async for text in text_chunks(source):
            size += len(text) + 1
            if size > deps.injection.limits.max_chars:
                raise deny("SECURITY_LIMIT_EXCEEDED")
            texts.append(text)
        for text in ("\n".join(texts),):
            evidence = (
                await run.scanner.observe_source(text)
                if run.scanner
                else await detector.scan(text)
            )
            summaries.append(evidence.safe_summary())
            run.set(
                security_report=run.result.security_report.model_copy(
                    update={"injection": tuple(summaries)}
                )
            )
            if len(summaries) > deps.injection.max_run_scans:
                raise deny("SECURITY_LIMIT_EXCEEDED")
            if evidence.action is InjectionAction.BLOCK:
                raise deny("PROMPT_INJECTION_DETECTED")
            if evidence.action is InjectionAction.NEEDS_REVIEW:
                await run.stop("LLM_SECURITY_REVIEW_REQUIRED")
        run.set(
            security_report=run.result.security_report.model_copy(
                update={"injection": tuple(summaries)}
            )
        )
        provider: LLMProvider | None = None
        if (
            routing
            and routing.mode is not LLMRoutingMode.NO_LLM
            and deps.providers
            and deps.parsing.mode.value != "deterministic"
            and saved_plan is None
        ):
            if len(routing.routes) != 1 or len(deps.providers) != 1:
                raise deny("LLM_DATA_ROUTING_FORBIDDEN")
            deployment = deps.providers[0]
            route = routing.routes[0]
            caps = deployment.capabilities
            if (
                canonical_sha256_value(caps) != route.capabilities_fingerprint
                or privacy.classification not in route.allowed_classifications
                or caps.tool_calling
                or (
                    privacy.classification is DataClassification.RESTRICTED
                    and caps.execution_environment is not LLMExecutionEnvironment.LOCAL
                )
                or (
                    (
                        routing.mode is LLMRoutingMode.LOCAL_ONLY
                        or (
                            routing.mode is LLMRoutingMode.PRIVACY_FIRST
                            and privacy.classification
                            in {
                                DataClassification.CONFIDENTIAL,
                                DataClassification.RESTRICTED,
                            }
                        )
                        or any(
                            item.action is InjectionAction.LOCAL_ONLY
                            for item in summaries
                        )
                    )
                    and caps.execution_environment is not LLMExecutionEnvironment.LOCAL
                )
            ):
                raise deny("LLM_DATA_ROUTING_FORBIDDEN")
            provider = LLMRunProvider(
                deployment, routing.budget, clock=deps.clock, resources=run.resources
            )
        run.semantic = SemanticParsingSession(
            replay=source.replay,
            context=context,
            policy=deps.parsing,
            provider=provider,
            scanner=run if run.scanner else None,
            clock=deps.clock,
        )
        if saved_plan:
            analysis = await HybridStructureAnalyzer(
                context=context, policy=deps.parsing, clock=deps.clock
            ).validate_saved(source.replay, saved_plan)
        else:
            try:
                analysis = await run.semantic.analyze_structure()
            finally:
                if isinstance(provider, LLMRunProvider):
                    run.set(provider_metadata=provider.calls)
        if any(report.decision == "blocked" for report in run.scans):
            raise deny("LLM_UNSAFE_CONTENT")
        if any(report.decision in {"review", "error"} for report in run.scans):
            await run.stop("LLM_SECURITY_REVIEW_REQUIRED")
        run.set(
            structure_profile=analysis.profile,
            provider_metadata=analysis.provider_metadata,
        )
        if run.resources.events and analysis.issues:
            run.set(
                parse_plan=analysis.plan,
                parse_plan_fingerprint=analysis.plan.fingerprint
                if analysis.plan
                else None,
            )
            await run.stop(analysis.issues[0].code)
        return analysis

    return await run.perform(S.STRUCTURE_ANALYZING, semantic)


async def validate(
    source: SourceAnalysis, plan: ParsePlan
) -> ParsePlanValidationResult:
    run = source._run

    async def check() -> ParsePlanValidationResult:
        if run.semantic is None:
            await run.stop("PARSE_PLAN_REPLAY_REQUIRED")
        assert run.semantic is not None
        result = await run.semantic.validate_parse_plan(plan)
        run.set(parse_validation=result)
        if result.validated_plan is not None:
            run.set(parse_plan=plan, parse_plan_fingerprint=plan.fingerprint)
        return result

    return await run.perform(S.PARSE_PLAN_VALIDATING, check)


async def execute(source: SourceAnalysis, plan: ParsePlan) -> NormalizedData:
    run = source._run
    checked = await validate(source, plan)
    if checked.validated_plan is None:
        await run.stop(
            checked.issues[0].code if checked.issues else "PARSE_PLAN_INVALID"
        )
    assert checked.validated_plan is not None
    verified = checked.validated_plan

    async def parse() -> NormalizedData:
        assert run.semantic is not None
        batches: list[NormalizedBatch] = []
        stream = run.semantic.parse_semantically(plan=plan)
        try:
            async for batch in stream:
                bounded_size(batch, run.dependencies.max_snapshot_bytes)
                run.retain(len(batch.model_dump_json().encode()))
                if len(batches) >= run.dependencies.max_batches:
                    raise deny("SECURITY_LIMIT_EXCEEDED")
                batches.append(batch)
        finally:
            await stream.aclose()
            report = run.semantic.report
            if report:
                run.set(
                    semantic_parse_report=report,
                    provider_metadata=report.provider_metadata,
                )
        if not report or report.status not in {S.COMPLETED, S.COMPLETED_WITH_WARNINGS}:
            await run.stop(
                report.issues[0].code
                if report and report.issues
                else "NEEDS_SEMANTIC_ANALYSIS"
            )
        if not batches or batches[-1].manifest is None:
            await run.stop("PROVENANCE_INVALID")
        manifest = batches[-1].manifest
        assert manifest is not None and report is not None
        manifest.validate_batches(batches)
        run.set(normalized_fingerprint=manifest.normalized_fingerprint)
        return NormalizedData(
            source=source,
            batches=tuple(batches),
            manifest=manifest,
            report=report,
            plan=verified,
        )

    return await run.perform(S.SEMANTIC_PARSING, parse)
