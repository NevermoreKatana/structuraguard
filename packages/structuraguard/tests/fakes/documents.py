"""Малые text-layer документы и exact quotes; ни сети, ни модели в тестах."""

from structuraguard.contracts.document_semantics import (
    DocumentEntityProposal,
    DocumentEntitySuggestion,
    DocumentFieldProposal,
    QuotedSpan,
)
from structuraguard.contracts.semantic import ParsingPolicy
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.llm import ScriptedResponse
from structuraguard.ports.parser import Parser
from structuraguard.structure.chunking import ChunkedSource, DocumentChunk
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import stream


async def physical(
    parser: Parser, content: bytes, *, batch_size: int = 100
) -> tuple[ExtractedBatch, ...]:
    source = source_for(content, display_name="semantic_fixture")
    return await collect(
        parser, source, contexts_for(source, content, batch_size=batch_size)[1]
    )


def suggestion(chunk: DocumentChunk) -> DocumentEntitySuggestion:
    entities = []
    for i, fragment in enumerate(chunk.fragments):
        text = fragment.text
        spans = QuotedSpan(ref=f"r{i}", start=0, end=len(text), quote=text)
        fields = []
        for name, value in (
            ("supplier", "Ada"),
            ("customer", "Bob"),
            ("fee", "1200"),
            ("date", "2026-09-10"),
        ):
            if value in text:
                start = text.index(value)
                fields.append(
                    DocumentFieldProposal(
                        name=name,
                        semantic_type="string",
                        spans=(
                            QuotedSpan(
                                ref=f"r{i}",
                                start=start,
                                end=start + len(value),
                                quote=value,
                            ),
                        ),
                    )
                )
        if not fields:
            fields.append(
                DocumentFieldProposal(
                    name="text", semantic_type="string", spans=(spans,)
                )
            )
        entities.append(
            DocumentEntityProposal(
                entity_id=f"e{i}",
                entity_type="clause",
                anchor=spans,
                parent_entity_id=None,
                fields=tuple(fields),
            )
        )
    return DocumentEntitySuggestion(
        schema_version="1.0.0",
        chunk_fingerprint=chunk.fingerprint,
        entities=tuple(entities),
        unresolved_refs=(),
    )


async def chunks_for(
    batches: tuple[ExtractedBatch, ...], policy: ParsingPolicy
) -> list[DocumentChunk]:
    manifest = batches[-1].manifest
    assert manifest is not None
    return [
        chunk
        async for chunk in ChunkedSource(manifest, policy).chunks(
            lambda: stream(batches)
        )
    ]


async def document_script(
    batches: tuple[ExtractedBatch, ...], policy: ParsingPolicy
) -> tuple[ScriptedResponse, ...]:
    return tuple(
        ScriptedResponse(
            output_json=suggestion(chunk).canonical_json(),
            input_tokens=20,
            output_tokens=30,
        )
        for chunk in await chunks_for(batches, policy)
    )
