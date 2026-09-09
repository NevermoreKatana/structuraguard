"""Отдельный inert adapter для physical Markdown blocks."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal

from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    LineRangeLocation,
    PhysicalMetadataEntry,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import (
    MarkdownParserLimits,
    PhysicalLine,
    PhysicalUnit,
    UnitBudget,
    advisory_signals,
    batch_units,
    binary_container_probe,
    decode_probe_sample,
    extracted_line,
    is_text_like,
    iter_physical_lines,
    limit_error,
    probe_lines,
    read_probe_sample,
)

_MEDIA_TYPES = frozenset({"text/markdown", "text/x-markdown"})
_EXTENSIONS = frozenset({".md", ".markdown", ".mdown", ".mkd"})
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+|$)")
_LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+")
_FENCE_START = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?:[^`~].*)?$")
_MAX_PROBE_LINES = 256


def _fence_marker(line: str) -> str | None:
    matched = _FENCE_START.match(line)
    return matched.group("marker") if matched is not None else None


def _is_fence_close(line: str, marker: str) -> bool:
    stripped = line.lstrip(" ")
    indentation = len(line) - len(stripped)
    if indentation > 3 or not stripped.startswith(marker[0]):
        return False
    run_length = len(stripped) - len(stripped.lstrip(marker[0]))
    return run_length >= len(marker) and not stripped[run_length:].strip(" \t")


def _has_markdown_structure(
    lines: tuple[str, ...],
    *,
    allow_inline_links: bool,
) -> bool:
    headings = sum(_HEADING.match(line) is not None for line in lines)
    fences = sum(_fence_marker(line) is not None for line in lines)
    list_items = sum(_LIST_ITEM.match(line) is not None for line in lines)
    inline_links = sum(
        1 for line in lines if "](" in line and "[" in line.partition("](")[0]
    )
    return (
        headings > 0
        or fences > 0
        or list_items >= 2
        or (allow_inline_links and inline_links >= 2)
    )


def _has_markdown_advisory(source: SourceArtifact) -> bool:
    declared = source.media_type.partition(";")[0].strip().casefold()
    _, separator, suffix = source.display_name.rpartition(".")
    return declared in _MEDIA_TYPES or (
        bool(separator) and f".{suffix.casefold()}" in _EXTENSIONS
    )


def _block_kind(physical_kind: str) -> ExtractedBlockKind:
    return {
        "heading": ExtractedBlockKind.HEADING,
        "blank": ExtractedBlockKind.LINE,
        "paragraph": ExtractedBlockKind.PARAGRAPH,
        "list": ExtractedBlockKind.LIST,
        "fenced_code": ExtractedBlockKind.EXTENSION,
    }[physical_kind]


class MarkdownParser:
    """Извлечь Markdown markers, lines и blocks без rendering или выполнения кода.

    Args:
        limits: Неизменяемые MarkdownParserLimits; ``None`` выбирает defaults.

    Probe проверяет bounded structural markers. Parse использует reader/лимиты
    context и возвращает ExtractedBatch с line/block provenance. Code fences и
    HTML остаются данными; извлечение не является полным CommonMark renderer.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Ошибка кодировки (``PARSER_ENCODING_UNSUPPORTED``) или ввода
            (``PARSER_MALFORMED_INPUT``), без replacement decoding.
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.markdown"
    version = "1.0.0"
    priority = 10

    def __init__(self, *, limits: MarkdownParserLimits | None = None) -> None:
        self._limits = limits or MarkdownParserLimits()
        if type(self._limits) is not MarkdownParserLimits:
            raise ValueError("limits должен быть экземпляром MarkdownParserLimits")

    @property
    def limits(self) -> MarkdownParserLimits:
        """Вернуть immutable instance-local resource limits."""

        return self._limits

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Подтвердить Markdown только по bounded structural markers."""

        sample = await read_probe_sample(source, context, self._limits)
        binary = binary_container_probe(
            sample,
            source=source,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
        )
        if binary is not None:
            return binary
        text, detection = decode_probe_sample(
            sample,
            source_size=source.size_bytes,
            limits=self._limits,
        )
        lines = probe_lines(text, max_lines=_MAX_PROBE_LINES)
        supported = is_text_like(text) and _has_markdown_structure(
            lines,
            allow_inline_links=_has_markdown_advisory(source),
        )
        structure = ProbeSignal(
            kind=ProbeSignalKind.INTERNAL_STRUCTURE,
            outcome=(
                ProbeSignalOutcome.MATCH
                if supported
                else ProbeSignalOutcome.INCONCLUSIVE
            ),
        )
        advisory = advisory_signals(
            source,
            media_types=_MEDIA_TYPES,
            extensions=_EXTENSIONS,
        )
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
            supported=supported,
            confidence=detection.confidence if supported else Decimal("0"),
            detected_media_type="text/markdown" if supported else None,
            detected_encoding=detection.reported_encoding,
            warnings=detection.warnings,
            format_id="md" if supported else None,
            signals=(structure, *advisory),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть physical lines/blocks без rendering, HTML или code execution."""

        return self._parse(source, context)

    async def _parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        async for batch in batch_units(
            self._units(source, context),
            source,
            context,
            adapter_id=self.adapter_id,
            version=self.version,
            parser_limits=self._limits,
        ):
            yield batch

    def _block_unit(
        self,
        source: SourceArtifact,
        lines: tuple[PhysicalLine, ...],
        physical_kind: str,
        *,
        block_order: int,
        is_source_last: bool,
        budget: UnitBudget,
    ) -> PhysicalUnit:
        physical_count = len(lines) + 1
        budget.ensure_pending(record_count=1, physical_count=physical_count)
        first = lines[0]
        last = lines[-1]
        block = ExtractedBlock(
            block_id=f"block-{block_order + 1}",
            kind=_block_kind(physical_kind),
            order=block_order,
            location=LineRangeLocation(
                source=source.ref,
                line_start=first.number,
                line_end=last.number,
            ),
            text="".join(line.raw_text for line in lines),
            metadata=(
                PhysicalMetadataEntry(
                    key="physical_kind",
                    value=physical_kind,
                ),
            ),
        )
        unit = PhysicalUnit(
            lines=tuple(extracted_line(source, line) for line in lines),
            blocks=(block,),
            record_count=1,
            is_source_last=is_source_last,
        )
        budget.commit(record_count=1, physical_count=physical_count)
        return unit

    def _check_block_size(self, current_chars: int, line: PhysicalLine) -> int:
        next_chars = current_chars + len(line.raw_text)
        if next_chars > self._limits.max_block_chars:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="block_chars",
                limit=self._limits.max_block_chars,
            )
        return next_chars

    async def _units(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[PhysicalUnit]:
        current: list[PhysicalLine] = []
        current_kind: str | None = None
        current_chars = 0
        active_fence: str | None = None
        block_order = 0
        budget = UnitBudget.from_context(self.adapter_id, context)

        async for line in iter_physical_lines(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
        ):
            if active_fence is not None:
                budget.ensure_pending(
                    record_count=1,
                    physical_count=len(current) + 2,
                )
                current_chars = self._check_block_size(current_chars, line)
                current.append(line)
                if _is_fence_close(line.text, active_fence) or line.is_source_last:
                    yield self._block_unit(
                        source,
                        tuple(current),
                        "fenced_code",
                        block_order=block_order,
                        is_source_last=line.is_source_last,
                        budget=budget,
                    )
                    block_order += 1
                    current.clear()
                    current_kind = None
                    current_chars = 0
                    active_fence = None
                    if line.is_source_last:
                        return
                continue

            marker = _fence_marker(line.text)
            if marker is not None:
                if current:
                    yield self._block_unit(
                        source,
                        tuple(current),
                        current_kind or "paragraph",
                        block_order=block_order,
                        is_source_last=False,
                        budget=budget,
                    )
                    block_order += 1
                    current.clear()
                    current_chars = 0
                current_kind = "fenced_code"
                active_fence = marker
                budget.ensure_pending(record_count=1, physical_count=2)
                current_chars = self._check_block_size(0, line)
                current.append(line)
                if line.is_source_last:
                    yield self._block_unit(
                        source,
                        tuple(current),
                        "fenced_code",
                        block_order=block_order,
                        is_source_last=True,
                        budget=budget,
                    )
                    return
                continue

            immediate_kind: str | None = None
            if not line.text.strip():
                immediate_kind = "blank"
            elif _HEADING.match(line.text) is not None:
                immediate_kind = "heading"

            if immediate_kind is not None:
                if current:
                    yield self._block_unit(
                        source,
                        tuple(current),
                        current_kind or "paragraph",
                        block_order=block_order,
                        is_source_last=False,
                        budget=budget,
                    )
                    block_order += 1
                    current.clear()
                    current_chars = 0
                self._check_block_size(0, line)
                yield self._block_unit(
                    source,
                    (line,),
                    immediate_kind,
                    block_order=block_order,
                    is_source_last=line.is_source_last,
                    budget=budget,
                )
                block_order += 1
                current_kind = None
                if line.is_source_last:
                    return
                continue

            line_kind = (
                "list" if _LIST_ITEM.match(line.text) is not None else "paragraph"
            )
            if current and current_kind != line_kind:
                yield self._block_unit(
                    source,
                    tuple(current),
                    current_kind or "paragraph",
                    block_order=block_order,
                    is_source_last=False,
                    budget=budget,
                )
                block_order += 1
                current.clear()
                current_chars = 0
            current_kind = line_kind
            budget.ensure_pending(
                record_count=1,
                physical_count=len(current) + 2,
            )
            current_chars = self._check_block_size(current_chars, line)
            current.append(line)
            if line.is_source_last:
                yield self._block_unit(
                    source,
                    tuple(current),
                    current_kind,
                    block_order=block_order,
                    is_source_last=True,
                    budget=budget,
                )
                return

        if current:
            yield self._block_unit(
                source,
                tuple(current),
                current_kind or "paragraph",
                block_order=block_order,
                is_source_last=True,
                budget=budget,
            )


__all__ = ("MarkdownParser", "MarkdownParserLimits")
