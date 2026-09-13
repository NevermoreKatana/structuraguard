"""Fake parser/provider/DB проходят реальную M10 proposal и M11 validation."""

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import FakeDatabase, defaults, engine, request
from tests.fakes.semantic import Scanner
from tests.fakes.semantic_mapping import decision_for

from structuraguard.contracts import DataClassification, SemanticParsingMode
from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.deterministic_mapping import MappingWeights
from structuraguard.contracts.llm import (
    LLMBudget,
    LLMErrorCode,
    LLMRoutePolicy,
    LLMRoutingMode,
    LLMRoutingPolicy,
)
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.llm import FakeLLMProvider, ScriptedFailure, ScriptedResponse
from structuraguard.mapping import prepare_semantic_mapping
from structuraguard.parsing import ParsingPolicy


class DeferredProvider:
    def __init__(self) -> None:
        self._caps = FakeLLMProvider((), clock=fixed_clock).capabilities.model_copy(
            update={"supported_purposes": ("semantic_parsing", "semantic_mapping")}
        )
        self.delegate = FakeLLMProvider((), clock=fixed_clock, capabilities=self._caps)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        return await self.delegate.generate_structured(request)


@pytest.mark.anyio
@pytest.mark.parametrize("unavailable", (False, True))
async def test_mapping_llm_result_reaches_load_report(unavailable: bool) -> None:
    db = FakeDatabase()
    provider = DeferredProvider()
    original = defaults(db)
    ranking = original.ranking.model_copy(
        update={
            "auto_threshold": Decimal(1),
            "weights": MappingWeights(
                name_similarity=Decimal("0.95"),
                structural_context=Decimal(0),
                alias_match=Decimal("0.05"),
                type_compatibility=Decimal(0),
                value_pattern_match=Decimal(0),
                database_relation_score=Decimal(0),
            ),
        }
    )
    deps = replace(
        original,
        parsing=ParsingPolicy(mode=SemanticParsingMode.LLM_ASSISTED),
        ranking=ranking,
        scanner=Scanner(),
        providers=(provider,),
        clock=fixed_clock,
        routing=LLMRoutingPolicy(
            policy_id="test",
            mode=LLMRoutingMode.FIXED,
            routes=(
                LLMRoutePolicy(
                    capabilities_fingerprint=canonical_sha256_value(
                        provider.capabilities
                    ),
                    allowed_classifications=tuple(DataClassification),
                ),
            ),
            budget=LLMBudget(max_calls=4, max_tokens=50000, max_time_ms=30000),
        ),
    )
    sdk = engine(dependencies=deps)
    source = await sdk.inspect_source(request())
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan
        data = await sdk.parse_semantically(source, plan=plan)
        profile = await sdk.profile_records(data)
        catalog = await sdk.inspect_database(source=data)
        prepared = await prepare_semantic_mapping(
            profile,
            catalog,
            scope=db.policy.mapping_policy.scope,
            ranking_options=ranking,
        )
        provider.delegate = FakeLLMProvider(
            tuple(
                ScriptedResponse(output_json=decision_for(group).canonical_json())
                for group in prepared.groups
            ),
            clock=fixed_clock,
            capabilities=provider.capabilities,
        )
        if unavailable:
            from structuraguard.pipeline.session import PipelineError

            provider.delegate = FakeLLMProvider(
                (ScriptedFailure(code=LLMErrorCode.UNAVAILABLE),),
                clock=fixed_clock,
                capabilities=provider.capabilities,
            )
            with pytest.raises(PipelineError) as captured:
                await sdk.create_mapping_plan(data)
            assert captured.value.result.status is S.FAILED
            assert captured.value.error_code == LLMErrorCode.UNAVAILABLE
            assert provider.delegate.call_count == 1
            assert not db.writes
            return
        proposal = await sdk.create_mapping_plan(data)
        assert proposal.semantic is not None
        assert provider.delegate.call_count == 1
        assert proposal.plan is not None, proposal.semantic
        result = await sdk.execute(data, plan=proposal.plan, dry_run=True)
        assert result.status is S.COMPLETED, result.errors
        assert result.provider_metadata and result.security_report.scans
        assert not db.writes
