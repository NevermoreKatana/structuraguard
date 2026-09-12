"""Недостающее сквозное покрытие критериев M10 на публичной границе mapper."""

import asyncio
from decimal import Decimal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import (
    RecordingScanner,
    decision_for,
    fake_provider,
    mapping_options,
    related_profile,
    router_for,
    run_mapper,
)

from structuraguard.contracts.common import IntegerScalar, PipelineStatus
from structuraguard.contracts.database import ForeignKeyCatalog
from structuraguard.contracts.llm import LLMBudget
from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.contracts.semantic_mapping import (
    SemanticMappingContext,
    SemanticMappingOptions,
)
from structuraguard.exceptions import LLMProviderError, MappingError
from structuraguard.llm import PolicyAwareLLMRouter
from structuraguard.mapping import (
    DeterministicMapper,
    LLMSemanticMapper,
    prepare_semantic_mapping,
)
from structuraguard.mapping._semantic_confidence import aggregate

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("connected", [False, True])
async def test_only_parent_child_groups_entities_into_one_request(
    connected: bool,
) -> None:
    data = await related_profile(connected=connected)
    db = catalog(
        table("customers", column("customer_key", "integer")),
        table(
            "orders",
            column("customer_ref", "integer"),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="customer_fk",
                    column_ids=("customer_ref",),
                    referenced_table_id="public.customers",
                    referenced_column_ids=("customer_key",),
                ),
            ),
        ),
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    assert len(prepared.groups) == (1 if connected else 2)
    assert sum(len(g.relations) for g in prepared.groups) == int(connected)
    provider = fake_provider(
        *(decision_for(g, split=True).canonical_json() for g in prepared.groups)
    )
    scanner = RecordingScanner()
    result = await LLMSemanticMapper(
        router=router_for(provider),
        scanner=scanner,
        context=SemanticMappingContext(run_id="mapping-run"),
        ranking_options=mapping_options(),
    ).propose(data, db, scope=scope_for(db))
    assert provider.call_count == len(scanner.requests) == len(result.groups)
    assert all(len(g.calls) == 1 for g in result.groups)


async def test_semantic_top_k_is_a_ceiling_even_with_larger_ranking_top_k() -> None:
    data = await profile()
    db = catalog(
        table(
            "contacts",
            column("email"),
            column("email_address", position=1),
            column("contact_email", position=2),
        )
    )
    prepared = await prepare_semantic_mapping(
        data,
        db,
        scope=scope_for(db),
        ranking_options=mapping_options(),
        options=SemanticMappingOptions(top_k=1),
    )
    assert len(prepared.groups[0].columns) == 1
    assert prepared.groups[0].fields[0].ranked.competitor_count == 3


@pytest.mark.parametrize("limit", ["max_entities", "max_groups"])
async def test_group_limit_rejects_before_any_scanner_or_provider(limit: str) -> None:
    data = await related_profile(connected=limit == "max_entities")
    db = catalog(
        table(
            "contacts",
            column("customer_key", "integer"),
            column("customer_ref", "integer", position=1),
        )
    )
    provider, scanner = fake_provider("{}"), RecordingScanner()
    mapper = LLMSemanticMapper(
        router=router_for(provider),
        scanner=scanner,
        context=SemanticMappingContext(run_id="mapping-run"),
        options=SemanticMappingOptions.model_validate({limit: 1}),
    )
    with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED"):
        await mapper.propose(data, db, scope=scope_for(db))
    assert not scanner.requests and provider.call_count == 0


async def test_empty_admissible_set_is_explicit_without_egress() -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")))
    provider, scanner = fake_provider("{}"), RecordingScanner()
    result = await LLMSemanticMapper(
        router=router_for(provider),
        scanner=scanner,
        context=SemanticMappingContext(run_id="mapping-run"),
    ).propose(
        data, db, scope=scope_for(db).model_copy(update={"deny": scope_for(db).allow})
    )
    assert result.status is PipelineStatus.NEEDS_REVIEW and result.action == "reject"
    assert result.groups[0].reasons == ("NO_ADMISSIBLE_CANDIDATES",)
    assert not result.groups[0].choices and result.groups[0].decision is None
    assert not scanner.requests and provider.call_count == 0


async def test_second_group_cannot_reset_run_budget_or_return_partial_success() -> None:
    data = await related_profile(connected=False)
    db = catalog(
        table(
            "contacts",
            column("customer_key", "integer"),
            column("customer_ref", "integer", position=1),
        )
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    provider = fake_provider(
        *(decision_for(g).canonical_json() for g in prepared.groups)
    )
    policy = router_for(provider).policy.model_copy(
        update={"budget": LLMBudget(max_calls=1, max_tokens=20000, max_time_ms=30000)}
    )
    router = PolicyAwareLLMRouter(
        policy=policy,
        providers=(provider,),
        run_id="mapping-run",
        clock=fixed_clock,
        monotonic=lambda: 0.0,
    )
    mapper = LLMSemanticMapper(
        router=router,
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
        ranking_options=mapping_options(),
    )
    with pytest.raises(LLMProviderError, match="LLM_BUDGET_EXCEEDED"):
        await mapper.propose(data, db, scope=scope_for(db))
    assert provider.call_count == len(router.calls) == 1


async def test_self_fk_cannot_be_auto_approved_by_maximal_llm_score() -> None:
    data = await profile(
        {"node_key": IntegerScalar(value=1), "parent_key": IntegerScalar(value=1)}
    )
    db = catalog(
        table(
            "nodes",
            column("node_key", "integer"),
            column("parent_key", "integer", position=1),
            foreign_keys=(
                ForeignKeyCatalog(
                    foreign_key_id="self_fk",
                    column_ids=("parent_key",),
                    referenced_table_id="public.nodes",
                    referenced_column_ids=("node_key",),
                ),
            ),
        )
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    assert group.relations[0].requires_strategy
    output = (
        decision_for(group, split=True).canonical_json().replace("0.990000", "1.000000")
    )
    result, _, _ = await run_mapper(data, db, output)
    assert result.status is PipelineStatus.NEEDS_REVIEW and result.action != "auto"
    assert "FK_STRATEGY_REQUIRED" in result.groups[0].reasons
    assert next(
        c for c in result.groups[0].choices if c.source_id == group.relation_sources[0]
    ).scores[0].validation_penalty == Decimal("0.10")


async def test_m10_preserves_m9_result_and_input_fingerprints() -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")))
    scope = scope_for(db)
    before = (data.canonical_json(), db.canonical_json(), scope.canonical_json())
    ranked = await DeterministicMapper(mapping_options()).rank(data, db, scope=scope)
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope, ranking_options=mapping_options()
    )
    result, _, _ = await run_mapper(data, db, decision_for(prepared.groups[0]))
    assert result.deterministic == prepared.deterministic == ranked
    assert (
        data.canonical_json(),
        db.canonical_json(),
        scope.canonical_json(),
    ) == before


class TimeoutScanner(RecordingScanner):
    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        raise TimeoutError("SCANNER_TIMEOUT_CANARY")


async def test_scanner_timeout_is_typed_without_egress_or_raw_details() -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")))
    provider = fake_provider("{}")
    mapper = LLMSemanticMapper(
        router=router_for(provider),
        scanner=TimeoutScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
    )
    with pytest.raises(LLMProviderError, match="LLM_TIMEOUT") as error:
        await mapper.propose(data, db, scope=scope_for(db))
    assert "SCANNER_TIMEOUT_CANARY" not in str(error.value)
    assert provider.call_count == 0


async def test_cancellation_during_scan_never_enters_provider_and_releases_mapper() -> (
    None
):
    entered, resume = asyncio.Event(), asyncio.Event()

    class PausedScanner(RecordingScanner):
        async def scan(self, request: SecurityScanRequest) -> SecurityReport:
            entered.set()
            await resume.wait()
            return await super().scan(request)

    data = await profile()
    db = catalog(table("contacts", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    provider = fake_provider(decision_for(prepared.groups[0]).canonical_json())
    router = router_for(provider)
    mapper = LLMSemanticMapper(
        router=router,
        scanner=PausedScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
        ranking_options=mapping_options(),
    )
    task = asyncio.create_task(mapper.propose(data, db, scope=scope_for(db)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.call_count == router.reserved_tokens == 0
    resume.set()
    assert (await mapper.propose(data, db, scope=scope_for(db))).groups[
        0
    ].decision is not None


@pytest.mark.parametrize(
    ("score", "ambiguous"), [("0.750000", False), ("0.749990", True)]
)
async def test_gap_equal_to_margin_is_distinct_from_gap_below_margin(
    score: str, ambiguous: bool
) -> None:
    data = await profile()
    db = catalog(
        table("customers", column("email")), table("suppliers", column("email"))
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    group = prepared.groups[0]
    decision = decision_for(group)
    choice = decision.columns[0]
    decision = decision.model_copy(
        update={
            "columns": (
                choice.model_copy(
                    update={
                        "assessments": tuple(
                            a.model_copy(
                                update={
                                    "semantic_score": score
                                    if a.candidate_id == choice.selected_candidate_id
                                    else "0.250000"
                                }
                            )
                            for a in choice.assessments
                        )
                    }
                ),
            )
        }
    )
    choices, _, _, _ = aggregate(group, decision, SemanticMappingOptions())
    selected = next(c for c in choices if c.source_id == choice.source_id)
    assert selected.ambiguous is ambiguous
    assert selected.action == ("confirm" if ambiguous else "auto")
