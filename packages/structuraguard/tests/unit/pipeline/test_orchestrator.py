"""M15: публичный поток и наблюдаемые gates до внешних side effects."""

import asyncio

import pytest

from structuraguard import AsyncStructuraGuard, StructuraGuard, StructuraGuardError
from structuraguard.contracts import PipelineStatus, SemanticParsingMode
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SDKDependencies, SourceRequest


class Stream:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    async def read(self, size: int) -> bytes:
        result = self.data[self.offset : self.offset + size]
        self.offset += len(result)
        return result


def sdk() -> AsyncStructuraGuard:
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    return AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=SDKDependencies(
            security=SecurityPolicy(allowed_formats=("csv",), parser_trust="trusted"),
            parsing=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        ),
    )


@pytest.mark.anyio
async def test_technical_and_semantic_stages_preserve_lineage() -> None:
    engine = sdk()
    source = await engine.inspect_source(
        SourceRequest(
            stream=Stream(b"name,n\nAda,1\nBob,2\n"), display_name="input.csv"
        )
    )
    assert source.manifest.extraction_fingerprint
    assert source.status is PipelineStatus.TECHNICAL_PARSING
    structure = await engine.analyze_structure(source)
    plan = await engine.create_parse_plan(source, structure=structure)
    assert plan is not None
    checked = await engine.validate_parse_plan(source, plan=plan)
    assert checked.validated_plan is not None
    normalized = await engine.parse_semantically(source, plan=plan)
    assert normalized.report.records == 2
    assert normalized.report.llm_calls == 0
    profile = await engine.profile_records(normalized)
    assert (
        profile.normalized_manifest_fingerprint
        == normalized.manifest.normalized_fingerprint
    )
    await source.aclose()
    with pytest.raises(StructuraGuardError, match="SOURCE_SNAPSHOT_EXPIRED"):
        await engine.analyze_structure(source)


@pytest.mark.anyio
async def test_default_policy_denies_before_parser() -> None:
    result = await AsyncStructuraGuard().ingest(
        SourceRequest(stream=Stream(b"name\nAda\n"), display_name="input.csv")
    )
    assert result.status is PipelineStatus.REJECTED_SECURITY
    assert result.load_report is None
    assert result.errors


@pytest.mark.anyio
async def test_sync_does_not_create_coroutine_in_running_loop() -> None:
    with pytest.raises(StructuraGuardError, match="SYNC_API_IN_ASYNC_CONTEXT"):
        StructuraGuard().ingest(SourceRequest(stream=Stream(b"")))
    assert asyncio.get_running_loop().is_running()
