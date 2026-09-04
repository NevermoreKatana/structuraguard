"""Управляемые parser fakes без реализации реального формата."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from typing import cast

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ProbeResult,
    SourceArtifact,
    SourceArtifactRef,
)
from structuraguard.ports.source import ParseContext, ProbeContext

type ProbeFactory = Callable[
    [SourceArtifact, ProbeContext],
    Awaitable[ProbeResult],
]


def _fingerprint(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeSourceReader:
    """In-memory bounded reader для unit/contract tests."""

    def __init__(self, source_fingerprint: str, *, content: bytes = b"") -> None:
        self._source_fingerprint = source_fingerprint
        self._content = bytes(content)
        self._reads: list[tuple[int, int]] = []

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    @property
    def reads(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._reads)

    async def read(self, *, offset: int, size: int) -> bytes:
        self._reads.append((offset, size))
        return self._content[offset : offset + size]


class FakeParser:
    """Программируемый technical parser для unit и общих contract tests."""

    def __init__(
        self,
        *,
        adapter_id: str = "fake.parser",
        version: str = "1.0.0",
        priority: int = 0,
        probe_result: ProbeResult | None = None,
        probe_factory: ProbeFactory | None = None,
        batches: tuple[object, ...] = (),
        probe_error: BaseException | None = None,
        parse_call_error: BaseException | None = None,
        parse_error: BaseException | None = None,
    ) -> None:
        if probe_result is not None and probe_factory is not None:
            raise ValueError(
                "probe_result и probe_factory взаимно исключают друг друга"
            )
        self._adapter_id = adapter_id
        self._version = version
        self._priority = priority
        self._probe_result = probe_result
        self._probe_factory = probe_factory
        self._batches = batches
        self._probe_error = probe_error
        self._parse_call_error = parse_call_error
        self._parse_error = parse_error
        self._probe_call_count = 0
        self._parse_call_count = 0

    @property
    def adapter_id(self) -> str:
        return self._adapter_id

    @property
    def version(self) -> str:
        return self._version

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def probe_call_count(self) -> int:
        return self._probe_call_count

    @property
    def parse_call_count(self) -> int:
        return self._parse_call_count

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        self._probe_call_count += 1
        if self._probe_error is not None:
            raise self._probe_error
        if self._probe_factory is not None:
            return await self._probe_factory(source, context)
        if self._probe_result is not None:
            return self._probe_result
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
            supported=False,
            confidence=Decimal("0"),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        self._parse_call_count += 1
        if self._parse_call_error is not None:
            raise self._parse_call_error
        return self._iterate_batches()

    async def _iterate_batches(self) -> AsyncIterator[ExtractedBatch]:
        if self._parse_error is not None:
            raise self._parse_error
        for batch in self._batches:
            yield cast(ExtractedBatch, batch)


def valid_extracted_batches(
    source: SourceArtifact | SourceArtifactRef,
    *,
    parser_id: str = "fake.parser",
    parser_version: str = "1.0.0",
    batch_count: int = 1,
    extraction_id: str = "extraction-1",
) -> tuple[ExtractedBatch, ...]:
    """Создать детерминированную valid physical batch sequence без raw values."""

    if type(batch_count) is not int or batch_count <= 0:
        raise ValueError("batch_count должен быть положительным int")
    source_ref = source.ref if isinstance(source, SourceArtifact) else source
    batch_fingerprints = tuple(
        _fingerprint(
            f"{source_ref.source_fingerprint}:{extraction_id}:"
            f"{parser_id}:{parser_version}:{batch_index}"
        )
        for batch_index in range(batch_count)
    )
    summaries = tuple(
        ExtractedBatchSummary(
            batch_index=batch_index,
            batch_fingerprint=batch_fingerprints[batch_index],
            source=source_ref,
            extraction_id=extraction_id,
            parser_id=parser_id,
            parser_version=parser_version,
            physical_ref_count=0,
        )
        for batch_index in range(batch_count)
    )
    manifest = ExtractedDatasetManifest(
        source=source_ref,
        extraction_id=extraction_id,
        parser_id=parser_id,
        parser_version=parser_version,
        batches=summaries,
        extraction_fingerprint=_fingerprint(
            f"{source_ref.source_fingerprint}:{extraction_id}:"
            f"{parser_id}:{parser_version}:manifest"
        ),
        source_index=ExtractedSourceIndex(),
    )
    return tuple(
        ExtractedBatch(
            extraction_id=extraction_id,
            batch_index=batch_index,
            source=source_ref,
            parser_id=parser_id,
            parser_version=parser_version,
            batch_fingerprint=batch_fingerprints[batch_index],
            is_last=batch_index == batch_count - 1,
            manifest=manifest if batch_index == batch_count - 1 else None,
        )
        for batch_index in range(batch_count)
    )
