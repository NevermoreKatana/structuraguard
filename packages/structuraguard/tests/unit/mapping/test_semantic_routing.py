"""Mapper сохраняет M6 privacy, cancellation и один бюджет на группы run."""

import asyncio

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.mapping import (
    catalog,
    column,
    profile,
    rehash_profile,
    scope_for,
    table,
)
from tests.fakes.semantic_mapping import (
    RecordingScanner,
    decision_for,
    fake_provider,
    mapping_options,
    router_for,
)

from structuraguard.contracts.common import (
    DataClassification,
    IntegerScalar,
    PipelineStatus,
)
from structuraguard.contracts.llm import LLMRoutingMode
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.semantic_mapping import SemanticMappingContext
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.mapping import LLMSemanticMapper, prepare_semantic_mapping

pytestmark = pytest.mark.anyio


async def test_no_llm_has_explicit_outcome_without_scan_or_generation() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    provider, scanner = fake_provider(), RecordingScanner()
    mapper = LLMSemanticMapper(
        router=router_for(provider, mode=LLMRoutingMode.NO_LLM),
        scanner=scanner,
        context=SemanticMappingContext(run_id="mapping-run"),
    )
    result = await mapper.propose(data, db, scope=scope_for(db))
    assert result.groups[0].decision is None
    assert result.groups[0].reasons == ("LLM_DISABLED",)
    assert result.status is PipelineStatus.NEEDS_REVIEW
    assert provider.call_count == 0 and not scanner.payloads


async def test_classification_is_not_downgraded_after_masking() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    provider = fake_provider(decision_for(prepared.groups[0]).canonical_json())
    scanner = RecordingScanner()
    mapper = LLMSemanticMapper(
        router=router_for(provider),
        scanner=scanner,
        context=SemanticMappingContext(
            run_id="mapping-run",
            data_classification=DataClassification.RESTRICTED,
            metadata_classification=DataClassification.PUBLIC,
        ),
        ranking_options=mapping_options(),
    )
    result = await mapper.propose(data, db, scope=scope_for(db))
    assert result.classification is DataClassification.RESTRICTED
    assert scanner.requests[0].data_classification is DataClassification.RESTRICTED
    assert "person@example.org" not in scanner.payloads[0]


async def test_two_groups_share_router_history_and_reservations() -> None:
    data = await profile(
        {"alpha": IntegerScalar(value=1), "beta": IntegerScalar(value=2)}
    )
    fields = []
    for f in data.fields:
        ref = SemanticFieldRef(
            entity_type=f.field.field_name, field_name=f.field.field_name
        )
        fields.append(
            f.model_copy(
                update={"field": ref, "pii": f.pii.model_copy(update={"field": ref})}
            )
        )
    data = rehash_profile(data, fields=tuple(fields), relationships=())
    db = catalog(
        table("first", column("alpha", "integer")),
        table("second", column("beta", "integer")),
    )
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    assert len(prepared.groups) == 2
    provider = fake_provider(
        *(decision_for(g).canonical_json() for g in prepared.groups)
    )
    router = router_for(provider)
    mapper = LLMSemanticMapper(
        router=router,
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
        ranking_options=mapping_options(),
    )
    result = await mapper.propose(data, db, scope=scope_for(db))
    assert [g.calls[0].attempt for g in result.groups] == [1, 2]
    assert len(router.calls) == provider.call_count == 2
    assert router.reserved_tokens == 2 * (8192 + 2048)


async def test_cancellation_and_concurrent_propose_do_not_retry() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def checkpoint() -> None:
        entered.set()
        await blocker.wait()

    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json=decision_for(prepared.groups[0]).canonical_json(),
                input_tokens=1,
                output_tokens=1,
            ),
        ),
        clock=fixed_clock,
        capabilities=fake_provider().capabilities,
        before_response=checkpoint,
    )
    router = router_for(provider)
    mapper = LLMSemanticMapper(
        router=router,
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
        ranking_options=mapping_options(),
    )
    task = asyncio.create_task(mapper.propose(data, db, scope=scope_for(db)))
    await entered.wait()
    with pytest.raises(LLMProviderError, match="LLM_BUDGET_EXCEEDED"):
        await mapper.propose(data, db, scope=scope_for(db))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.call_count == len(router.calls) == 1
    assert router.reserved_tokens == 10240
