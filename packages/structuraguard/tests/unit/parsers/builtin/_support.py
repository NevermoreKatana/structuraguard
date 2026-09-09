"""Минимальные helpers для contract tests встроенных text parsers."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

from tests.fakes.parsers import FakeSourceReader

from structuraguard.contracts import (
    ExtractedBatch,
    LineRangeLocation,
    PhysicalMetadataEntry,
    SourceArtifact,
    SourceLocation,
)
from structuraguard.ports import Parser
from structuraguard.ports.source import (
    BatchOptions,
    ParseContext,
    ProbeContext,
    SourceReader,
)


class ShortReadSourceReader:
    """SourceReader, принудительно дробящий multibyte и newline boundaries."""

    def __init__(
        self,
        source_fingerprint: str,
        content: bytes,
        *,
        max_chunk_bytes: int,
    ) -> None:
        self._source_fingerprint = source_fingerprint
        self._content = content
        self._max_chunk_bytes = max_chunk_bytes

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        read_size = min(size, self._max_chunk_bytes)
        return self._content[offset : offset + read_size]


def source_for(
    content: bytes,
    *,
    display_name: str,
    media_type: str = "text/plain",
) -> SourceArtifact:
    """Создать fingerprint-bound artifact для точного байтового payload."""

    fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
    return SourceArtifact(
        artifact_id="source-1",
        display_name=display_name,
        media_type=media_type,
        size_bytes=len(content),
        source_fingerprint=fingerprint,
    )


def contexts_for(
    source: SourceArtifact,
    content: bytes,
    *,
    batch_size: int = 1_000,
    max_batches: int = 10_000,
    max_bytes: int | None = None,
    reader: SourceReader | None = None,
    detected_encoding: str | None = None,
) -> tuple[ProbeContext, ParseContext]:
    """Создать независимые bounded contexts поверх одного snapshot."""

    selected_reader = reader or FakeSourceReader(
        source.source_fingerprint,
        content=content,
    )
    return (
        ProbeContext(
            reader=selected_reader,
            source_fingerprint=source.source_fingerprint,
            max_probe_bytes=max(1, len(content)),
        ),
        ParseContext(
            reader=selected_reader,
            source_fingerprint=source.source_fingerprint,
            max_bytes=max_bytes or max(1, len(content)),
            max_records=1_000,
            max_nesting_depth=16,
            batch_options=BatchOptions(
                batch_size=batch_size,
                max_batches=max_batches,
            ),
            max_physical_objects=10_000,
            detected_encoding=detected_encoding,
        ),
    )


async def collect(
    parser: Parser,
    source: SourceArtifact,
    context: ParseContext,
) -> tuple[ExtractedBatch, ...]:
    """Материализовать только малый unit-test stream."""

    stream: AsyncIterator[ExtractedBatch] = parser.parse(source, context)
    return tuple([batch async for batch in stream])


def metadata_map(
    entries: tuple[PhysicalMetadataEntry, ...],
) -> dict[str, str | int | float | bool | None]:
    """Преобразовать bounded metadata tuple для точных assertions."""

    return {entry.key: entry.value for entry in entries}


def line_span(location: SourceLocation) -> tuple[int, int]:
    """Проверить и вернуть one-based line provenance."""

    assert isinstance(location, LineRangeLocation)
    return location.line_start, location.line_end


def physical_projection(
    batches: tuple[ExtractedBatch, ...],
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    """Сравнить physical output, игнорируя batch-local IDs и fingerprints."""

    lines: list[object] = []
    blocks: list[object] = []
    for batch in batches:
        for line in batch.lines:
            location = line.location
            assert isinstance(location, LineRangeLocation)
            lines.append(
                (
                    line.line_number,
                    line.text,
                    location.line_start,
                    location.line_end,
                    location.column_start,
                    location.column_end,
                    tuple(metadata_map(line.metadata).items()),
                )
            )
        for block in batch.blocks:
            location = block.location
            assert isinstance(location, LineRangeLocation)
            values: list[object] = []
            for value in block.values:
                value_location = value.location
                assert isinstance(value_location, LineRangeLocation)
                values.append(
                    (
                        value.raw_value.model_dump(mode="json"),
                        value.technical_type_hint,
                        value_location.line_start,
                        value_location.line_end,
                        value_location.column_start,
                        value_location.column_end,
                    )
                )
            blocks.append(
                (
                    block.kind,
                    block.order,
                    block.text,
                    location.line_start,
                    location.line_end,
                    location.column_start,
                    location.column_end,
                    tuple(metadata_map(block.metadata).items()),
                    tuple(values),
                )
            )
    return tuple(lines), tuple(blocks)
