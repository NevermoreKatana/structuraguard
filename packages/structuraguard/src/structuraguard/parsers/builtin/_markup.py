"""Bounded I/O, counters и batches; грамматики markup здесь не смешиваются."""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import cast

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.common import PhysicalSourceRef, ProducerMetadata
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedBlock,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ExtractedTable,
    ExtractedTreeNode,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers._hashing import batch_fingerprint, manifest_fingerprint
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import (
    _read_checked,
    advisory_signals,
    encoding_error,
    extraction_id,
    limit_error,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkupLimits:
    """Конечные общие пределы; каждый adapter владеет своим экземпляром."""

    read_chunk_bytes: int = 4096
    max_probe_bytes: int = 65536
    max_depth: int = 64
    max_nodes: int = 1_000_000
    max_text_chars: int = 16_000_000
    max_value_chars: int = 1_000_000
    max_name_chars: int = 1024
    max_token_chars: int = 65536
    max_subtree_nodes: int = 10000
    max_subtree_chars: int = 4_000_000
    max_batch_nodes: int = 20000
    max_batch_chars: int = 8_000_000

    def __post_init__(self) -> None:
        caps = {
            "read_chunk_bytes": 4096,
            "max_probe_bytes": 1048576,
            "max_depth": 128,
            "max_nodes": 10_000_000,
            "max_text_chars": 100_000_000,
            "max_value_chars": 1048576,
            "max_name_chars": 4096,
            "max_token_chars": 1048576,
            "max_subtree_nodes": 100000,
            "max_subtree_chars": 32_000_000,
            "max_batch_nodes": 200000,
            "max_batch_chars": 64_000_000,
        }
        for name, cap in caps.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"{name} должен быть int от 1 до {cap}")


def malformed(reason: str, *, line_number: int = 1) -> ParserError:
    """Не включать parser snippets, URI или raw source в исключение."""

    return ParserError(
        error_code="PARSER_MALFORMED_INPUT",
        message="Некорректный markup.",
        details={"reason": reason, "line_number": line_number},
    )


def rejected(reason: str) -> SecurityPolicyError:
    """Запрещённая capability, а не попытка её безопасного исполнения."""

    return SecurityPolicyError(
        error_code="SECURITY_INPUT_REJECTED",
        message="Содержимое запрещено политикой parser.",
        details={"reason": reason},
    )


def missing_extra(extra: str) -> ParserError:
    return ParserError(
        error_code="PARSER_DEPENDENCY_UNAVAILABLE",
        message="Требуется optional parser extra.",
        details={"extra": extra},
    )


class Budget:
    """Проверки выполняются до добавления узлов и фрагментов в buffers."""

    def __init__(
        self, adapter_id: str, limits: MarkupLimits, context: ParseContext
    ) -> None:
        self.adapter_id = adapter_id
        self.limits = limits
        self.context = context
        self.nodes = 0
        self.text_chars = 0

    def check(self, resource: str, value: int, limit: int) -> None:
        if value > limit:
            raise limit_error(
                adapter_id=self.adapter_id, resource=resource, limit=limit
            )

    def node(self, *, depth: int, name: str = "") -> None:
        self.check(
            "nesting_depth",
            depth,
            min(self.limits.max_depth, self.context.max_nesting_depth),
        )
        self.check("name_chars", len(name), self.limits.max_name_chars)
        self.check(
            "nodes",
            self.nodes + 1,
            min(self.limits.max_nodes, self.context.max_physical_objects),
        )
        self.nodes += 1

    def text(self, size: int, *, existing: int = 0) -> None:
        self.check("value_chars", existing + size, self.limits.max_value_chars)
        self.check("text_chars", self.text_chars + size, self.limits.max_text_chars)
        self.text_chars += size


async def byte_chunks(
    source: SourceArtifact, context: ParseContext, budget: Budget
) -> AsyncIterator[bytes]:
    """Чтение snapshot с short reads, byte budget и cancellation checkpoints."""

    budget.check("bytes", source.size_bytes, context.max_bytes)
    offset = 0
    while offset < source.size_bytes:
        await asyncio.sleep(0)
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=min(budget.limits.read_chunk_bytes, source.size_bytes - offset),
        )
        if not chunk:
            raise malformed("unexpected_eof")
        offset += len(chunk)
        yield chunk


async def utf8_chunks(
    source: SourceArtifact, context: ParseContext, budget: Budget
) -> AsyncIterator[str]:
    """HTML/YAML: явная strict UTF-8 policy, BOM допустим, fallback отсутствует."""

    if context.detected_encoding not in {None, "utf-8", "utf-8-sig", "ascii"}:
        raise encoding_error(
            reason="unsupported_codec", encoding=context.detected_encoding
        )
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    async for chunk in byte_chunks(source, context, budget):
        try:
            text = decoder.decode(chunk)
        except UnicodeDecodeError:
            raise encoding_error(reason="decode_failed", encoding="utf-8") from None
        yield text
    try:
        tail = decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise encoding_error(reason="decode_failed", encoding="utf-8") from None
    if tail:
        yield tail


async def probe_bytes(
    source: SourceArtifact, context: ProbeContext, limits: MarkupLimits
) -> bytes:
    target = min(source.size_bytes, context.max_probe_bytes, limits.max_probe_bytes)
    data = bytearray()
    while len(data) < target:
        await asyncio.sleep(0)
        chunk = await _read_checked(
            context.reader,
            offset=len(data),
            size=min(limits.read_chunk_bytes, target - len(data)),
        )
        if not chunk:
            raise malformed("unexpected_eof")
        data.extend(chunk)
    return bytes(data)


def probe_result(
    source: SourceArtifact,
    *,
    adapter_id: str,
    supported: bool,
    format_id: str,
    media_types: frozenset[str],
    extensions: frozenset[str],
    encoding: str | None = None,
    warnings: tuple[str, ...] = (),
    signal_kind: ProbeSignalKind = ProbeSignalKind.INTERNAL_STRUCTURE,
) -> ProbeResult:
    return ProbeResult(
        source=source.ref,
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        supported=supported,
        confidence=Decimal("0.90") if supported else Decimal("0"),
        format_id=format_id if supported else None,
        detected_encoding=encoding,
        detected_media_type={
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pdf": "application/pdf",
            "xml": "application/xml",
            "html": "text/html",
            "yaml": "application/yaml",
        }[format_id]
        if supported
        else None,
        warnings=warnings,
        signals=(
            ProbeSignal(
                kind=signal_kind,
                outcome=ProbeSignalOutcome.MATCH
                if supported
                else ProbeSignalOutcome.INCONCLUSIVE,
            ),
            *advisory_signals(source, media_types=media_types, extensions=extensions),
        ),
    )


@dataclass(frozen=True, slots=True)
class MarkupUnit:
    trees: tuple[ExtractedTreeNode, ...]
    blocks: tuple[ExtractedBlock, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    records: int = 1

    @property
    def chars(self) -> int:
        return (
            sum(
                len(n.value.raw_value.value)
                for n in self.trees
                if n.value is not None and isinstance(n.value.raw_value.value, str)
            )
            + sum(
                len(b.text or "")
                + sum(len(line.text) for line in b.lines)
                + sum(
                    len(v.raw_value.value)
                    for v in b.values
                    if isinstance(v.raw_value.value, str)
                )
                for b in self.blocks
            )
            + sum(
                len(c.value.raw_value.value)
                for t in self.tables
                for c in t.cells
                if isinstance(c.value.raw_value.value, str)
            )
        )

    @property
    def size(self) -> int:
        return (
            len(self.trees)
            + sum(1 + len(b.lines) + len(b.values) for b in self.blocks)
            + sum(1 + len(t.cells) for t in self.tables)
        )


@dataclass(slots=True)
class MarkupDocument:
    root: ExtractedTreeNode | None = None


async def markup_batches(
    source: SourceArtifact,
    context: ParseContext,
    budget: Budget,
    document: MarkupDocument,
    units: AsyncGenerator[MarkupUnit],
    *,
    unit_batch_size: int | None = None,
    adapter_options_fingerprint: str | None = None,
) -> AsyncIterator[ExtractedBatch]:
    """Общие manifest/continuation правила без знания грамматики форматов."""

    option_values: dict[str, CanonicalInput] = {
        "limits": cast(CanonicalInput, asdict(budget.limits)),
        "batch_options": cast(CanonicalInput, asdict(context.batch_options)),
    }
    if adapter_options_fingerprint is not None:
        option_values["adapter_options_fingerprint"] = adapter_options_fingerprint
    options = canonical_sha256_value(
        cast(
            CanonicalInput,
            option_values,
        )
    )
    run_id = extraction_id(
        source,
        adapter_id=budget.adapter_id,
        version="1.0.0",
        options_fingerprint=options,
    )
    summaries: list[ExtractedBatchSummary] = []
    refs: list[PhysicalSourceRef] = []
    pending: list[MarkupUnit] = []
    nodes = chars = records = physical = child_start = segment_index = 0

    def build(*, is_last: bool) -> ExtractedBatch:
        nonlocal physical, child_start, segment_index
        index = len(summaries)
        budget.check(
            "batches", index + (1 if is_last else 2), context.batch_options.max_batches
        )
        trees = tuple(n for unit in pending for n in unit.trees)
        if document.root is not None:
            root = document.root
            count = sum(n.parent_id == root.node_id for n in trees)
            if count or is_last:
                segment = root.model_copy(
                    update={
                        "tree_id": root.node_id,
                        "segment_index": segment_index,
                        "child_start_index": child_start,
                        "child_count": count,
                        "is_last_segment": is_last,
                    }
                )
                # Prolog/epilog XML — siblings document element, не его children.
                position = next(
                    (
                        i
                        for i, n in enumerate(trees)
                        if n.parent_id == root.node_id
                        or (n.parent_id is None and n.order > root.order)
                    ),
                    len(trees),
                )
                trees = (*trees[:position], segment, *trees[position:])
                child_start += count
                segment_index += 1
        draft = ExtractedBatch(
            schema_version="1.1.0",
            extraction_id=run_id,
            batch_index=index,
            source=source.ref,
            parser_id=budget.adapter_id,
            parser_version="1.0.0",
            batch_fingerprint="sha256:" + "0" * 64,
            trees=trees,
            blocks=tuple(b for u in pending for b in u.blocks),
            tables=tuple(t for u in pending for t in u.tables),
            record_count=sum(u.records for u in pending),
        )
        summary = draft.to_summary()
        budget.check(
            "physical_objects",
            physical + summary.physical_ref_count,
            context.max_physical_objects,
        )
        physical += summary.physical_ref_count
        indexed = draft.physical_refs()[: max(0, 10000 - len(refs))]
        draft = draft.model_copy(update={"indexed_refs": indexed})
        fingerprint = batch_fingerprint(draft.model_copy(update={"is_last": is_last}))
        draft = draft.model_copy(update={"batch_fingerprint": fingerprint})
        summaries.append(draft.to_summary())
        refs.extend(indexed)
        manifest = None
        if is_last:
            manifest = ExtractedDatasetManifest(
                schema_version="1.1.0",
                source=source.ref,
                extraction_id=run_id,
                parser_id=budget.adapter_id,
                parser_version="1.0.0",
                producer=ProducerMetadata(
                    component_id=budget.adapter_id,
                    component_version="1.0.0",
                    sdk_version="0.3.0",
                ),
                parser_options_fingerprint=options,
                batches=tuple(summaries),
                extraction_fingerprint="sha256:" + "0" * 64,
                source_index=ExtractedSourceIndex(refs=tuple(refs)),
                record_count=records,
            )
            manifest = manifest.model_copy(
                update={"extraction_fingerprint": manifest_fingerprint(manifest)}
            )
        return ExtractedBatch.model_validate(
            {**draft.model_dump(), "is_last": is_last, "manifest": manifest}
        )

    try:
        async for unit in units:
            await asyncio.sleep(0)
            budget.check("records", records + unit.records, context.max_records)
            budget.check("subtree_nodes", unit.size, budget.limits.max_subtree_nodes)
            budget.check("subtree_chars", unit.chars, budget.limits.max_subtree_chars)
            if pending and (
                len(pending) >= (unit_batch_size or context.batch_options.batch_size)
                or nodes + unit.size + 1 > budget.limits.max_batch_nodes
                or chars + unit.chars > budget.limits.max_batch_chars
            ):
                yield build(is_last=False)
                pending.clear()
                nodes = chars = 0
            budget.check(
                "batch_nodes",
                nodes + unit.size + 1,
                budget.limits.max_batch_nodes,
            )
            budget.check(
                "batch_chars", chars + unit.chars, budget.limits.max_batch_chars
            )
            pending.append(unit)
            nodes += unit.size
            chars += unit.chars
            records += unit.records
        await asyncio.sleep(0)
        yield build(is_last=True)
    finally:
        await units.aclose()
