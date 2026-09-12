"""Regression финального review: deadline и provenance schema без внешних API."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import ValidationError
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import (
    RecordingScanner,
    decision_for,
    fake_provider,
    mapping_options,
    router_for,
)

from structuraguard.contracts import (
    LLMRoutingMode,
    SecurityReport,
    SecurityScanRequest,
    SemanticMappingContext,
    SemanticMappingOptions,
    SemanticMappingResult,
)
from structuraguard.contracts.semantic_mapping import ResponseRetention
from structuraguard.exceptions import LLMProviderError, MappingError
from structuraguard.mapping import (
    LLMSemanticMapper,
    _semantic_prompt,
    prepare_semantic_mapping,
    semantic,
    semantic_mapping_response_schema,
)
from structuraguard.mapping._inputs import bounded_size
from structuraguard.mapping._semantic_confidence import aggregate
from structuraguard.mapping._semantic_validation import validate_decision

pytestmark = pytest.mark.anyio


class AdvancingClock:
    """Изменять длительность реальной операции, сохраняя её входы и результат."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.offset = 0.0

    def __call__(self) -> float:
        return self.clock() + self.offset

    def after[**P, T](self, operation: Callable[P, T]) -> Callable[P, T]:
        def measured(*args: P.args, **kwargs: P.kwargs) -> T:
            result = operation(*args, **kwargs)
            self.offset += 2.0
            return result

        return measured


async def test_preparation_rejects_deadline_crossed_in_last_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    loop = asyncio.get_running_loop()
    clock = AdvancingClock(loop.time)
    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", clock)
        patch.setattr(
            _semantic_prompt,
            "group_payload",
            clock.after(_semantic_prompt.group_payload),
        )
        with pytest.raises(MappingError, match="MAPPING_LIMIT_EXCEEDED") as error:
            await prepare_semantic_mapping(
                data,
                db,
                scope=scope_for(db),
                options=SemanticMappingOptions(max_seconds=1),
            )
        assert error.value.details == {"reason": "semantic_preparation_deadline"}


@pytest.mark.parametrize(
    "stage", ["preparation", "schema", "scanner", "validation", "aggregation", "result"]
)
async def test_mapper_deadline_prevents_late_egress_or_success_and_releases_instance(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    ranked = mapping_options()
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=ranked
    )
    output = decision_for(prepared.groups[0]).canonical_json()
    provider = fake_provider(output, output)
    router = router_for(provider)
    loop = asyncio.get_running_loop()
    clock = AdvancingClock(loop.time)

    class ClockedScanner(RecordingScanner):
        async def scan(self, request: SecurityScanRequest) -> SecurityReport:
            report = await super().scan(request)
            if stage == "scanner" and len(self.requests) == 1:
                clock.offset += 2.0
            return report

    scanner = ClockedScanner()
    mapper = LLMSemanticMapper(
        router=router,
        scanner=scanner,
        context=SemanticMappingContext(run_id="mapping-run"),
        options=SemanticMappingOptions(max_seconds=1),
        ranking_options=ranked,
    )

    def measure_result(value: object, cap: int) -> int:
        result = bounded_size(value, cap)
        if isinstance(value, SemanticMappingResult):
            clock.offset += 2.0
        return result

    with monkeypatch.context() as patch:
        patch.setattr(loop, "time", clock)
        if stage == "preparation":
            patch.setattr(
                _semantic_prompt,
                "group_payload",
                clock.after(_semantic_prompt.group_payload),
            )
        elif stage == "schema":
            patch.setattr(
                semantic,
                "semantic_mapping_response_schema",
                clock.after(semantic_mapping_response_schema),
            )
        elif stage == "validation":
            patch.setattr(semantic, "validate_decision", clock.after(validate_decision))
        elif stage == "aggregation":
            patch.setattr(semantic, "aggregate", clock.after(aggregate))
        elif stage == "result":
            patch.setattr(semantic, "bounded_size", measure_result)
        with pytest.raises(LLMProviderError, match="LLM_TIMEOUT"):
            await mapper.propose(data, db, scope=scope_for(db))

    attempts = int(stage in {"validation", "aggregation", "result"})
    assert provider.call_count == len(router.calls) == attempts
    assert len(scanner.requests) == int(stage not in {"preparation", "schema"})
    reserved = router.reserved_tokens
    assert (reserved > 0) == bool(attempts)
    result = await mapper.propose(data, db, scope=scope_for(db))
    assert result.action == "auto"
    assert provider.call_count == len(router.calls) == attempts + 1
    assert router.reserved_tokens > reserved


@pytest.mark.parametrize("retention", ["validated_decision", "metadata_only"])
@pytest.mark.parametrize("mode", ["active", "no_llm", "empty_scope"])
async def test_schema_provenance_is_retained_without_raw_response(
    retention: ResponseRetention, mode: str
) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    scope = scope_for(db)
    if mode == "empty_scope":
        scope = scope.model_copy(update={"deny": scope.allow})
    ranked = mapping_options()
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope, ranking_options=ranked
    )
    provider = (
        fake_provider(decision_for(prepared.groups[0]).canonical_json())
        if mode == "active"
        else fake_provider()
    )
    mapper = LLMSemanticMapper(
        router=router_for(
            provider,
            mode=LLMRoutingMode.NO_LLM if mode == "no_llm" else LLMRoutingMode.FIXED,
        ),
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
        options=SemanticMappingOptions(response_retention=retention),
        ranking_options=ranked,
    )
    result = await mapper.propose(data, db, scope=scope)
    schema = semantic_mapping_response_schema()
    assert result.response_schema_id == schema.schema_id
    assert result.response_schema_version == schema.version
    assert result.response_schema_fingerprint == schema.fingerprint
    assert schema.fingerprint not in str(result.safe_summary())
    restored = SemanticMappingResult.model_validate_json(result.canonical_json())
    assert restored.response_schema_fingerprint == schema.fingerprint
    assert (restored.groups[0].decision is None) == (
        retention == "metadata_only" or mode != "active"
    )
    assert provider.call_count == int(mode == "active")


async def test_legacy_result_does_not_invent_schema_provenance() -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    result = await LLMSemanticMapper(
        router=router_for(fake_provider(), mode=LLMRoutingMode.NO_LLM),
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
    ).propose(data, db, scope=scope_for(db))
    legacy = result.model_dump(mode="json")
    for name in (
        "response_schema_id",
        "response_schema_version",
        "response_schema_fingerprint",
    ):
        legacy.pop(name, None)
    restored = SemanticMappingResult.model_validate(legacy)
    assert restored.response_schema_id is None
    assert restored.response_schema_version is None
    assert restored.response_schema_fingerprint is None
    assert restored.model_dump(mode="json") == legacy


@pytest.mark.parametrize(
    "missing",
    ["response_schema_id", "response_schema_version", "response_schema_fingerprint"],
)
async def test_result_rejects_partial_schema_provenance(missing: str) -> None:
    data = await profile()
    db = catalog(table("customers", column("email")))
    result = await LLMSemanticMapper(
        router=router_for(fake_provider(), mode=LLMRoutingMode.NO_LLM),
        scanner=RecordingScanner(),
        context=SemanticMappingContext(run_id="mapping-run"),
    ).propose(data, db, scope=scope_for(db))
    schema = semantic_mapping_response_schema()
    values = result.model_dump(mode="json")
    values.update(
        response_schema_id=schema.schema_id,
        response_schema_version=schema.version,
        response_schema_fingerprint=schema.fingerprint,
    )
    del values[missing]
    with pytest.raises(ValidationError):
        SemanticMappingResult.model_validate(values)
