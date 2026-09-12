"""Только тестовые approval/scoring fixtures; production scanner не подменяются."""

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    DataClassification,
    IntegerScalar,
    PipelineStatus,
    ProducerMetadata,
)
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.llm import (
    LLMBudget,
    LLMRoutePolicy,
    LLMRoutingMode,
    LLMRoutingPolicy,
)
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import FieldRelationship, NormalizedDataProfile
from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.contracts.semantic_mapping import (
    SemanticAssessment,
    SemanticChoice,
    SemanticMappingCandidateSet,
    SemanticMappingContext,
    SemanticMappingDecision,
    SemanticMappingResult,
    SemanticTableChoice,
)
from structuraguard.llm import FakeLLMProvider, PolicyAwareLLMRouter, ScriptedResponse
from structuraguard.mapping import LLMSemanticMapper
from tests.fakes.llm import fixed_clock
from tests.fakes.mapping import only_weight, profile, rehash_profile, scope_for


def mapping_options() -> DeterministicMappingOptions:
    return DeterministicMappingOptions(weights=only_weight("name_similarity"))


async def related_profile(*, connected: bool = True) -> NormalizedDataProfile:
    """Два entity types; co-occurrence не подменяет parent_child evidence."""
    data = await profile(
        {"customer_key": IntegerScalar(value=1), "customer_ref": IntegerScalar(value=1)}
    )
    fields = []
    for field in data.fields:
        ref = SemanticFieldRef(
            entity_type="customer"
            if field.field.field_name == "customer_key"
            else "order",
            field_name=field.field.field_name,
        )
        fields.append(
            field.model_copy(
                update={
                    "field": ref,
                    "pii": field.pii.model_copy(update={"field": ref}),
                }
            )
        )
    return rehash_profile(
        data,
        fields=tuple(fields),
        relationships=(
            FieldRelationship(
                left=fields[0].field,
                right=fields[1].field,
                count=25,
                kind="parent_child" if connected else "co_occurrence",
            ),
        ),
    )


class RecordingScanner:
    """Trusted тестовый scanner сохраняет только синтетические fixture payloads."""

    def __init__(self) -> None:
        self.payloads: list[str] = []
        self.requests: list[SecurityScanRequest] = []

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        self.payloads.append(request.payload_json)
        self.requests.append(request)
        return SecurityReport(
            request_id=request.request_id,
            run_id=request.run_id,
            purpose=request.purpose,
            content_fingerprint=request.content_fingerprint,
            payload_fingerprint=request.payload_fingerprint,
            data_classification=request.data_classification,
            routing_policy_id=request.routing_policy_id,
            routing_policy_fingerprint=request.routing_policy_fingerprint,
            redaction_fingerprint=request.redaction_fingerprint,
            producer=ProducerMetadata(
                component_id="test_scanner",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            decision="allowed",
            status=PipelineStatus.COMPLETED,
            artifact_fingerprints=(
                request.content_fingerprint,
                request.payload_fingerprint,
            ),
            scanned_items=1,
            generated_at=fixed_clock(),
        )


def router_for(
    provider: FakeLLMProvider, *, mode: LLMRoutingMode = LLMRoutingMode.FIXED
) -> PolicyAwareLLMRouter:
    policy = LLMRoutingPolicy(
        policy_id="mapping-local",
        mode=mode,
        routes=()
        if mode is LLMRoutingMode.NO_LLM
        else (
            LLMRoutePolicy(
                capabilities_fingerprint=canonical_sha256_value(provider.capabilities),
                allowed_classifications=tuple(DataClassification),
            ),
        ),
        budget=LLMBudget(max_calls=8, max_tokens=200000, max_time_ms=30000),
    )
    return PolicyAwareLLMRouter(
        policy=policy,
        providers=() if mode is LLMRoutingMode.NO_LLM else (provider,),
        run_id="mapping-run",
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    )


def fake_provider(*outputs: str) -> FakeLLMProvider:
    caps = FakeLLMProvider((), clock=fixed_clock).capabilities.model_copy(
        update={"supported_purposes": ("semantic_mapping",)}
    )
    return FakeLLMProvider(
        tuple(
            ScriptedResponse(output_json=s, input_tokens=100, output_tokens=100)
            for s in outputs
        ),
        clock=fixed_clock,
        capabilities=caps,
    )


def decision_for(
    group: SemanticMappingCandidateSet,
    *,
    preferred_table: str | None = None,
    split: bool = False,
) -> SemanticMappingDecision:
    selected_columns = []
    for field in group.fields:
        available = [c for c in group.columns if c.source_id == field.source_id]
        available.sort(
            key=lambda c: (
                c.mapping.target.table_id != preferred_table
                if preferred_table
                else False,
                -c.explanation.base_score,
                c.candidate_id,
            )
        )
        if available:
            selected_columns.append(available[0])
    selected_ids = {c.candidate_id for c in selected_columns}
    table_ids = {c.table_candidate_id for c in selected_columns}

    def assessments(
        ids: tuple[str, ...], selected: set[str]
    ) -> tuple[SemanticAssessment, ...]:
        return tuple(
            SemanticAssessment(
                candidate_id=x,
                semantic_score="0.990000" if x in selected else "0.010000",
                reason_code="SEMANTIC_MATCH",
            )
            for x in ids
        )

    tables = tuple(
        SemanticTableChoice(
            source_id=f"e{i}",
            status="selected"
            if any(
                t.source_id == f"e{i}" and t.candidate_id in table_ids
                for t in group.tables
            )
            else "unmapped",
            selected_candidate_ids=tuple(
                t.candidate_id
                for t in group.tables
                if t.source_id == f"e{i}" and t.candidate_id in table_ids
            ),
            assessments=assessments(
                tuple(t.candidate_id for t in group.tables if t.source_id == f"e{i}"),
                table_ids,
            ),
            reason_code="ENTITY_CONTEXT",
        )
        for i in range(len(group.entity_types))
    )
    columns = tuple(
        SemanticChoice(
            source_id=f.source_id,
            status="selected"
            if any(c.source_id == f.source_id for c in selected_columns)
            else "unmapped",
            selected_candidate_id=next(
                (
                    c.candidate_id
                    for c in selected_columns
                    if c.source_id == f.source_id
                ),
                None,
            ),
            assessments=assessments(
                tuple(
                    c.candidate_id for c in group.columns if c.source_id == f.source_id
                ),
                selected_ids,
            ),
            reason_code="SEMANTIC_MATCH",
        )
        for f in group.fields
    )
    relations = []
    for source in group.relation_sources:
        candidates = [r for r in group.relations if r.source_id == source]
        chosen = next(
            (
                r
                for r in candidates
                if all(
                    any(
                        a in selected_ids and b in selected_ids
                        for a, b in p.allowed_pairs
                    )
                    for p in r.pairs
                )
            ),
            None,
        )
        key = chosen.candidate_id if chosen and split else None
        relations.append(
            SemanticChoice(
                source_id=source,
                status="selected" if key else "unmapped",
                selected_candidate_id=key,
                assessments=assessments(
                    tuple(r.candidate_id for r in candidates), {key} if key else set()
                ),
                reason_code="RELATION_CONTEXT",
            )
        )
    return SemanticMappingDecision(
        schema_version="1.0.0",
        group_id=group.group_id,
        candidate_set_fingerprint=group.fingerprint,
        tables=tables,
        columns=columns,
        relations=tuple(relations),
        review_required=False,
    )


async def run_mapper(
    data: NormalizedDataProfile,
    db: DatabaseCatalog,
    decision: SemanticMappingDecision | str,
) -> tuple[SemanticMappingResult, FakeLLMProvider, RecordingScanner]:
    provider = fake_provider(
        decision if isinstance(decision, str) else decision.canonical_json()
    )
    scanner = RecordingScanner()
    mapper = LLMSemanticMapper(
        router=router_for(provider),
        scanner=scanner,
        context=SemanticMappingContext(
            run_id="mapping-run", metadata_classification=DataClassification.INTERNAL
        ),
        ranking_options=mapping_options(),
    )
    result = await mapper.propose(data, db, scope=scope_for(db))
    return result, provider, scanner
