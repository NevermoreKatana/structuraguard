"""Реальные format adapters → semantic parsing → format-neutral profiler."""

import pytest
from tests.unit.structure.test_execution import execution_context, prepared, stream

from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.profiling import NormalizedDataProfiler
from structuraguard.structure import ParsePlanExecutor, ParsePlanValidator

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    ("parser", "content"),
    [
        (DelimitedTextParser(), b"name,count\nAda,1\nBob,2\n"),
        (JsonDocumentParser(), b'[{"id":1,"items":[{"sku":"a"},{"sku":"b"}]}]'),
        (
            PlainTextParser(),
            b"metric cpu 1\n  detail one\nmetric cpu 2\n  detail two\n",
        ),
    ],
)
async def test_real_semantic_output_and_plan_replay(
    parser: Parser, content: bytes
) -> None:
    request, physical = await prepared(parser, content)
    validated = await ParsePlanValidator().validate_source(request, stream(physical))
    assert validated.validated_plan is not None
    first = await NormalizedDataProfiler().profile(
        ParsePlanExecutor().execute(
            stream(physical),
            validated.validated_plan,
            execution_context(request, batch_size=1),
        )
    )
    second = await NormalizedDataProfiler().profile(
        ParsePlanExecutor().execute(
            stream(physical),
            validated.validated_plan,
            execution_context(request, batch_size=1000),
        )
    )
    assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
    assert first.fields and first.record_count > 0
    assert first.fields == second.fields


async def test_m6_document_session_profiles_normalized_entities() -> None:
    from tests.fakes.documents import document_script, physical
    from tests.fakes.llm import fixed_clock
    from tests.fakes.semantic import Scanner, context

    from structuraguard.llm import FakeLLMProvider
    from structuraguard.parsers.builtin import MarkdownParser
    from structuraguard.parsing import ParsingPolicy, SemanticParsingSession

    batches = await physical(MarkdownParser(), b"Ada agrees with Bob.\n")
    provider = FakeLLMProvider(
        await document_script(batches, ParsingPolicy()), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        result = await NormalizedDataProfiler().profile(session.parse_semantically())
        assert session.report is not None
        assert (
            result.normalized_manifest_fingerprint
            == session.report.normalized_fingerprint
        )
        assert result.fields and result.value_count > 0
