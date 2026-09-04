"""Общий contract suite для будущих technical parser adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from structuraguard.contracts import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.parsers import ParserRegistry
from structuraguard.ports import Parser
from structuraguard.ports.source import ParseContext, ProbeContext


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserContractCase:
    """Один valid source case, который обязан поддержать parser adapter."""

    parser: Parser
    source: SourceArtifact
    probe_context: ProbeContext
    parse_context: ParseContext


async def assert_parser_contract(
    case: ParserContractCase,
) -> tuple[ExtractedBatch, ...]:
    """Проверить единый physical-output contract технического parser."""

    parser = case.parser
    assert isinstance(parser, Parser), "Adapter должен реализовать Parser protocol"
    assert type(parser.adapter_id) is str, "adapter_id должен быть точным str"
    assert type(parser.version) is str, "version должен быть точным str"
    assert type(parser.priority) is int, "priority должен быть точным int"
    assert case.probe_context.source_fingerprint == case.source.source_fingerprint
    assert case.parse_context.source_fingerprint == case.source.source_fingerprint

    registry = ParserRegistry()
    registry.register(parser)
    batches: list[ExtractedBatch] = []
    async with registry.session() as session:
        selected = await session.select(case.source, case.probe_context)
        result = selected.probe_result
        assert type(result) is ProbeResult, "probe обязан вернуть ProbeResult"
        assert result.supported, "Valid contract case должен поддерживаться parser"
        assert result.source == case.source.ref, (
            "ProbeResult принадлежит другому source"
        )
        assert result.adapter_id == parser.adapter_id
        assert result.adapter_version == parser.version

        stream = selected.parse(case.source, case.parse_context)
        assert isinstance(stream, AsyncIterator), "parse обязан вернуть AsyncIterator"
        async for batch in stream:
            assert type(batch) is ExtractedBatch, "Parser обязан вернуть ExtractedBatch"
            assert batch.batch_index == len(batches), (
                "Batch indices должны быть непрерывны"
            )
            assert batch.source == case.source.ref, "Batch принадлежит другому source"
            assert batch.parser_id == parser.adapter_id
            assert batch.parser_version == parser.version
            assert not batches or not batches[-1].is_last, (
                "После terminal batch есть output"
            )
            batches.append(batch)
        assert stream.completed, "Parser stream должен завершиться terminal manifest"

    assert batches, "Parser обязан вернуть terminal ExtractedBatch"
    terminal = batches[-1]
    assert terminal.is_last, "Последний ExtractedBatch должен быть terminal"
    assert terminal.manifest is not None, "Terminal ExtractedBatch требует manifest"
    terminal.manifest.validate_batches(batches)
    return tuple(batches)
