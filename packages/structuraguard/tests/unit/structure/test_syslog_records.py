"""Форма сообщения не меняет границы подтверждённых syslog raw records."""

import json
from decimal import Decimal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus, SemanticParsingMode
from structuraguard.contracts.parsing import StructurePlanCreated
from structuraguard.contracts.semantic import LLMStructurePolicy
from structuraguard.llm import FakeLLMProvider
from structuraguard.parsers.builtin import LogParser
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.structure import DeterministicStructureAnalyzer


@pytest.mark.anyio
@pytest.mark.parametrize("second_message", ["No jobs available", "word " * 70])
async def test_deterministic_syslog_keeps_all_records_with_variable_json_messages(
    second_message: str,
) -> None:
    lines = [
        f"2026-01-01T10:00:0{index + 1}Z node-a worker[{index + 1}]: "
        + json.dumps({"level": "INFO", "message": message})
        for index, message in enumerate(("Job started", second_message))
    ]
    content = ("\n".join(lines) + "\n").encode()
    source = source_for(content, display_name="events.log")
    batches = await collect(
        LogParser(), source, contexts_for(source, content, batch_size=1)[1]
    )
    analysis = await DeterministicStructureAnalyzer().analyze(stream(batches))
    assert isinstance(analysis, StructurePlanCreated)
    assert len(analysis.profile.candidates) == 1
    provider = FakeLLMProvider((), clock=fixed_clock)
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        policy=ParsingPolicy(
            mode=SemanticParsingMode.DETERMINISTIC,
            structural=LLMStructurePolicy(confidence_threshold=Decimal("0.9")),
        ),
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED
        assert [
            value.normalized_value.value
            for batch in output
            for record in batch.records
            for entity in record.entities
            for value in entity.values
        ] == lines
        assert provider.call_count == 0
