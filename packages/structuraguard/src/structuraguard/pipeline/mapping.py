"""Чистая assembly существующих candidates и независимые M11 gates."""

from dataclasses import dataclass
from decimal import Decimal

from structuraguard.contracts.common import LoadOperation
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.database import CatalogColumnRef, DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import DeterministicMappingResult
from structuraguard.contracts.llm import LLMRoutingMode
from structuraguard.contracts.mapping import (
    FieldMapping,
    MappingCandidate,
    MappingPlan,
    MappingPlanValidationResult,
)
from structuraguard.contracts.mapping_rules import MappingRelation
from structuraguard.contracts.mapping_validation import MappingPlanInputReport
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_mapping import (
    SemanticMappingContext,
    SemanticMappingResult,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.llm.router import PolicyAwareLLMRouter
from structuraguard.mapping import (
    DeterministicMapper,
    LLMSemanticMapper,
    MappingPlanValidator,
)

from .session import producer
from .source import NormalizedData


@dataclass(frozen=True)
class MappingProposal:
    """Результат create_mapping_plan без разрешения на DB operations.

    ``plan`` отсутствует при неполном/неоднозначном покрытии; причины остаются
    в ``candidates`` и необязательном LLM-результате ``semantic``. Готовый draft
    требует validate_mapping_plan. Создание DTO не выполняет I/O; metadata
    может содержать чувствительные labels и не предназначена для logs.
    """

    plan: MappingPlan | None
    candidates: DeterministicMappingResult
    semantic: SemanticMappingResult | None = None


async def create(
    data: NormalizedData,
    catalog: DatabaseCatalog,
    profile: NormalizedDataProfile,
    *,
    operation: LoadOperation,
) -> MappingProposal:
    run = data.source._run
    binding = run.database
    assert binding is not None
    policy = binding.policy.mapping_policy

    async def propose() -> MappingProposal:
        ranked = await DeterministicMapper(run.dependencies.ranking).rank(
            profile,
            catalog,
            scope=policy.scope,
            semantic_catalog=binding.semantic_catalog,
        )
        run.set(candidates=ranked)
        selected = tuple(
            field.candidates[0]
            for field in ranked.fields
            if field.status == "auto_candidate"
            and not field.ambiguous
            and field.candidates
        )
        semantic: SemanticMappingResult | None = None
        routing = run.dependencies.routing
        if (
            len(selected) != len(ranked.fields)
            and routing
            and routing.mode is not LLMRoutingMode.NO_LLM
            and run.dependencies.parsing.mode.value != "deterministic"
        ):
            if run.scanner is None:
                await run.stop("LLM_POLICY_DENIED")
            effective = run.resources.llm_policy(routing)
            router = PolicyAwareLLMRouter(
                policy=effective,
                providers=run.dependencies.providers,
                run_id=run.run_id,
                resources=run.resources,
                clock=run.dependencies.clock,
            )
            mapper = LLMSemanticMapper(
                router=router,
                scanner=run,
                context=SemanticMappingContext(
                    run_id=run.run_id, data_classification=profile.classification
                ),
                ranking_options=run.dependencies.ranking,
            )
            try:
                semantic = await mapper.propose(
                    profile,
                    catalog,
                    scope=policy.scope,
                    semantic_catalog=binding.semantic_catalog,
                )
            finally:
                run.set(
                    provider_metadata=(*run.result.provider_metadata, *router.calls)
                )
            run.set(semantic_mapping=semantic)
            if any(report.decision == "blocked" for report in run.scans):
                raise SecurityPolicyError(
                    error_code="LLM_UNSAFE_CONTENT", message="LLM_UNSAFE_CONTENT"
                )
            if (
                run.resources.events
                and run.resources.events[0].code == "SECURITY_OPERATION_FAILED"
            ):
                failed = next(
                    (
                        call.error_code
                        for call in reversed(router.calls)
                        if call.error_code
                    ),
                    None,
                )
                if failed:
                    await run.stop(failed)
            chosen: list[MappingCandidate] = []
            if semantic.action == "auto":
                for group in semantic.groups:
                    by_id = {
                        column.candidate_id: column.mapping
                        for column in group.candidates.columns
                    }
                    for choice in group.choices:
                        if choice.action == "auto" and not choice.ambiguous:
                            chosen.extend(
                                by_id[key]
                                for key in choice.selected_candidate_ids
                                if key in by_id
                            )
                selected = tuple(chosen)
        if (
            not ranked.fields
            or len(selected) != len(ranked.fields)
            or len({candidate.source for candidate in selected}) != len(ranked.fields)
        ):
            return MappingProposal(None, ranked, semantic)
        if len({candidate.target for candidate in selected}) != len(selected):
            return MappingProposal(None, ranked, semantic)
        mappings = tuple(
            FieldMapping(source=c.source, target=c.target, confidence=c.confidence)
            for c in selected
        )
        targets = {m.target: m.source for m in mappings}
        tables = {m.target.table_id for m in mappings}
        relations: list[MappingRelation] = []
        for schema in catalog.schemas:
            for table in schema.tables:
                if table.table_id not in tables:
                    continue
                for fk in table.foreign_keys:
                    refs = tuple(
                        CatalogColumnRef(table_id=table.table_id, column_id=cid)
                        for cid in fk.column_ids
                    )
                    if not all(ref in targets for ref in refs):
                        return MappingProposal(None, ranked, semantic)
                    relations.append(
                        MappingRelation(
                            foreign_key_id=fk.foreign_key_id,
                            child_table_id=table.table_id,
                            parent_table_id=fk.referenced_table_id,
                            child_column_ids=fk.column_ids,
                            parent_column_ids=fk.referenced_column_ids,
                            child_sources=tuple(targets[ref] for ref in refs),
                            strategy="source_values",
                        )
                    )
        manifest = data.manifest
        plan = MappingPlan(
            schema_version="1.1.0",
            plan_id=str(run.dependencies.new_id()),
            revision=1,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            parse_plan_fingerprint=manifest.parse_plan_fingerprint,
            normalized_fingerprint=manifest.normalized_fingerprint,
            database_fingerprint=catalog.database_fingerprint,
            target_id=catalog.target_id,
            target_policy_fingerprint=catalog.target_policy_fingerprint,
            mappings=mappings,
            operation=operation,
            confidence=min((c.confidence for c in selected), default=Decimal(0)),
            producer=producer(),
            relations=tuple(relations),
        )
        return MappingProposal(plan, ranked, semantic)

    proposal = await run.perform(S.MAPPING, propose)
    if proposal.plan:

        async def created() -> MappingProposal:
            run.set(
                mapping_plan=proposal.plan,
                mapping_plan_fingerprint=proposal.plan.fingerprint
                if proposal.plan
                else None,
            )
            return proposal

        await run.perform(S.MAPPING_PLAN_CREATED, created)
    return proposal


async def validate(
    data: NormalizedData,
    catalog: DatabaseCatalog,
    plan: MappingPlan,
    profile: NormalizedDataProfile,
) -> MappingPlanValidationResult | MappingPlanInputReport:
    run = data.source._run
    assert run.database is not None
    policy = run.database.policy.mapping_policy

    async def check() -> MappingPlanValidationResult | MappingPlanInputReport:
        data.manifest.validate_batches(data.batches)
        result = await MappingPlanValidator(
            policy=policy, clock=run.dependencies.clock
        ).validate(plan, data.manifest, catalog, profile=profile)
        run.set(
            mapping_plan=plan,
            mapping_plan_fingerprint=plan.fingerprint,
            mapping_validation=result,
        )
        return result

    return await run.perform(S.MAPPING_PLAN_VALIDATING, check)
