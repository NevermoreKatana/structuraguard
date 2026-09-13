"""M6 policies в SDK: bounded calls и отсутствие скрытой смены режима."""

from dataclasses import replace

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import Stream
from tests.fakes.semantic import Scanner, encoded, scenario

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts import DataClassification, SemanticParsingMode
from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.llm import (
    LLMBudget,
    LLMRoutePolicy,
    LLMRoutingMode,
    LLMRoutingPolicy,
)
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser, PlainTextParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SDKDependencies, SourceRequest


def deps(
    provider: FakeLLMProvider, mode: SemanticParsingMode, *, no_llm: bool = False
) -> SDKDependencies:
    return SDKDependencies(
        security=SecurityPolicy(allowed_formats=("csv", "txt"), parser_trust="trusted"),
        parsing=ParsingPolicy(mode=mode),
        providers=(provider,),
        scanner=Scanner(),
        clock=fixed_clock,
        routing=LLMRoutingPolicy(
            policy_id="test",
            mode=LLMRoutingMode.NO_LLM if no_llm else LLMRoutingMode.FIXED,
            routes=()
            if no_llm
            else (
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


@pytest.mark.anyio
@pytest.mark.parametrize("mode", tuple(SemanticParsingMode))
async def test_parsing_modes_with_fake_provider(mode: SemanticParsingMode) -> None:
    content = b"name,n\nAda,1\nBob,2\n"
    _, _, proposed = await scenario(DelimitedTextParser(), content)
    provider = FakeLLMProvider(
        (ScriptedResponse(output_json=encoded(proposed)),), clock=fixed_clock
    )
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry, dependencies=deps(provider, mode)
    )
    source = await sdk.inspect_source(
        SourceRequest(stream=Stream(content), display_name="input.csv")
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan is not None, source._run.result.errors
        result = await sdk.parse_semantically(source, plan=plan)
        assert result.report.records == 2
        assert provider.call_count == int(mode is SemanticParsingMode.LLM_FIRST)
        assert len(source._run.result.provider_metadata) == provider.call_count


@pytest.mark.anyio
async def test_no_llm_does_not_silently_change_llm_first() -> None:
    provider = FakeLLMProvider((), clock=fixed_clock)
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=deps(provider, SemanticParsingMode.LLM_FIRST, no_llm=True),
    )
    result = await sdk.ingest(
        SourceRequest(stream=Stream(b"name,n\nAda,one\n"), display_name="input.csv")
    )
    assert result.status is S.NEEDS_REVIEW
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_repeated_rows_do_not_consume_one_llm_call_per_row() -> None:
    provider = FakeLLMProvider((), clock=fixed_clock)
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=deps(provider, SemanticParsingMode.LLM_ASSISTED),
    )
    content = b"name,n\n" + b"Ada,1\n" * 100
    source = await sdk.inspect_source(
        SourceRequest(stream=Stream(content), display_name="input.csv")
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan
        data = await sdk.parse_semantically(source, plan=plan)
        assert data.report.records == 100
        assert provider.call_count == 0


@pytest.mark.anyio
async def test_unknown_structure_requires_review() -> None:
    provider = FakeLLMProvider((), clock=fixed_clock)
    registry = ParserRegistry()
    registry.register(PlainTextParser())
    configured = replace(
        deps(provider, SemanticParsingMode.DETERMINISTIC), routing=None
    )
    sdk = AsyncStructuraGuard(parser_registry=registry, dependencies=configured)
    result = await sdk.ingest(
        SourceRequest(
            stream=Stream(b"words without a record grammar"), display_name="input.txt"
        )
    )
    assert result.status is S.NEEDS_REVIEW
    assert result.errors[0].code == "NEEDS_SEMANTIC_ANALYSIS"
    assert result.parse_plan is None
    assert result.database_report is None
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_ambiguous_table_is_not_accepted_by_confidence_alone() -> None:
    provider = FakeLLMProvider((), clock=fixed_clock)
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=deps(provider, SemanticParsingMode.DETERMINISTIC),
    )
    result = await sdk.ingest(
        SourceRequest(
            stream=Stream(b"name,n\nAda,one\nBob,two\n"), display_name="input.csv"
        )
    )
    assert result.status is S.NEEDS_REVIEW
    assert result.errors[0].code == "NEEDS_SEMANTIC_ANALYSIS"
    assert result.parse_plan is None
    assert result.database_report is None
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_missing_scanner_never_calls_provider() -> None:
    provider = FakeLLMProvider((), clock=fixed_clock)
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=replace(
            deps(provider, SemanticParsingMode.LLM_FIRST), scanner=None
        ),
    )
    result = await sdk.ingest(
        SourceRequest(
            stream=Stream(b"name,n\nAda,1\nBob,2\n"), display_name="input.csv"
        )
    )
    assert result.status is S.NEEDS_REVIEW
    assert provider.call_count == 0


@pytest.mark.anyio
async def test_provider_failure_keeps_attempt_without_hidden_fallback() -> None:
    from structuraguard.contracts.llm import LLMErrorCode
    from structuraguard.llm import ScriptedFailure

    provider = FakeLLMProvider(
        (ScriptedFailure(code=LLMErrorCode.UNAVAILABLE),), clock=fixed_clock
    )
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=deps(provider, SemanticParsingMode.LLM_FIRST),
    )
    result = await sdk.ingest(
        SourceRequest(
            stream=Stream(b"name,n\nAda,1\nBob,2\n"), display_name="input.csv"
        )
    )
    assert result.status is S.NEEDS_REVIEW
    assert provider.call_count == 1
    assert result.provider_metadata[0].error_code is LLMErrorCode.UNAVAILABLE
    assert result.load_report is None
