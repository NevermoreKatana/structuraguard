"""Fake parser/provider/DB проходят реальную M10 proposal и M11 validation."""

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.mapping import catalog as make_catalog
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
    table = db.catalog.schemas[0].tables[0]
    db.catalog = make_catalog(
        table.model_copy(
            update={
                "columns": tuple(
                    column.model_copy(update={"name": label})
                    for column, label in zip(
                        table.columns, ("name", "city"), strict=True
                    )
                )
            }
        )
    )
    db.inspector.catalog = db.catalog
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
    source = await sdk.inspect_source(
        request(b'[{"name":"Ada","city":"Riga"},{"name":"Bob","city":"Oslo"}]')
    )
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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source_name", "score", "expected_action", "ambiguous"),
    [
        ("postal_codde", "0.980000", "auto", False),
        ("destination_zip", "0.980000", "auto", False),
        ("postal_codde", "0.940000", "confirm", False),
        ("postal_codde", "0.800000", "confirm", False),
        ("postal_codde", "0.500000", "reject", False),
        ("destination_zip", "0.980000", "confirm", True),
    ],
)
async def test_sdk_maps_different_names_with_validated_semantic_confidence(
    source_name: str, score: str, expected_action: str, ambiguous: bool
) -> None:
    from tests.fakes.mapping import column, scope_for

    from structuraguard.contracts.database import (
        ColumnCatalog,
        IndexCatalog,
        IndexKeyCatalog,
        TableCatalog,
        TableInspectionMetadata,
    )
    from structuraguard.contracts.parsing import TreePathSelector
    from structuraguard.contracts.semantic_mapping import SemanticMappingOptions

    db = FakeDatabase()
    columns: tuple[ColumnCatalog, ...] = (
        column("postal_code").model_copy(
            update={"nullable": False, "primary_key": True}
        ),
    )
    if ambiguous:
        columns += (column("postal_area", position=1),)
    db.catalog = make_catalog(
        TableCatalog(
            table_id="public.records",
            schema_name="public",
            name="records",
            columns=columns,
            primary_key=("postal_code",),
            inspection=TableInspectionMetadata(
                indexes=(
                    IndexCatalog(
                        index_id="pk",
                        name="pk",
                        unique=True,
                        origin="primary_key",
                        keys=(IndexKeyCatalog(column_id="postal_code", collation="C"),),
                    ),
                )
            ),
        )
    )
    db.inspector.catalog = db.catalog
    scope = scope_for(db.catalog)
    db.policy = db.policy.model_copy(
        update={
            "mapping_policy": db.policy.mapping_policy.model_copy(
                update={"scope": scope, "source_identity_allow": scope.allow}
            ),
            "read_policy": db.policy.read_policy.model_copy(
                update={"allow_columns": scope.allow}
            ),
        }
    )
    provider = DeferredProvider()
    deps = replace(
        defaults(db),
        parsing=ParsingPolicy(mode=SemanticParsingMode.LLM_ASSISTED),
        # Даже распознанная опечатка должна пройти настоящий semantic stage.
        ranking=defaults(db).ranking.model_copy(update={"auto_threshold": Decimal(1)}),
        semantic_mapping=SemanticMappingOptions(auto_threshold=Decimal("0.95")),
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
    source = await sdk.inspect_source(
        request(
            (
                '[{"' + source_name + '":"101000"},{"' + source_name + '":"420000"}]'
            ).encode()
        )
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan
        fields = []
        for field in plan.fields:
            assert isinstance(field.selector, TreePathSelector)
            fields.append(field.model_copy(update={"semantic_name": source_name}))
        payload = plan.model_dump(exclude={"fingerprint"})
        payload.update(
            fields=tuple(fields),
            entities=tuple(
                entity.model_copy(update={"entity_type": "records"})
                for entity in plan.entities
            ),
        )
        plan = type(plan).model_validate(payload)
        data = await sdk.parse_semantically(source, plan=plan)
        profile = await sdk.profile_records(data)
        catalog = await sdk.inspect_database(source=data)
        prepared = await prepare_semantic_mapping(
            profile,
            catalog,
            scope=scope,
            ranking_options=deps.ranking,
            options=deps.semantic_mapping,
        )
        provider.delegate = FakeLLMProvider(
            tuple(
                ScriptedResponse(
                    output_json=decision_for(group)
                    .canonical_json()
                    .replace("0.990000", score)
                    .replace("0.010000", score if ambiguous else "0.010000")
                )
                for group in prepared.groups
            ),
            clock=fixed_clock,
            capabilities=provider.capabilities,
        )
        proposal = await sdk.create_mapping_plan(data)
        assert provider.delegate.call_count == 1
        assert proposal.semantic is not None
        assert proposal.semantic.action == expected_action, proposal.semantic.groups[
            0
        ].reasons
        assert proposal.semantic.groups[0].confidence == Decimal(score) - (
            Decimal("0.10") if ambiguous else Decimal(0)
        )
        if expected_action != "auto":
            assert proposal.plan is None
            assert not db.writes
            return
        assert proposal.plan is not None
        mapping = proposal.plan.mappings[0]
        assert mapping.source.field_name == source_name
        assert mapping.target.column_id == "postal_code"
        assert mapping.confidence == proposal.plan.confidence == Decimal(score)
        result = await sdk.execute(data, plan=proposal.plan, dry_run=True)
        assert result.status is S.COMPLETED, result.errors
        assert not db.writes
