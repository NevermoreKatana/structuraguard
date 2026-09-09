"""Отдельный lossless adapter для plain text."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

from structuraguard.contracts.source import (
    ExtractedBatch,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import (
    PhysicalUnit,
    TextParserLimits,
    UnitBudget,
    advisory_signals,
    batch_units,
    binary_container_probe,
    decode_probe_sample,
    extracted_line,
    is_text_like,
    iter_physical_lines,
    read_probe_sample,
)

_MEDIA_TYPES = frozenset({"text/plain", "application/text"})
_EXTENSIONS = frozenset({".txt", ".text"})


class PlainTextParser:
    """Извлечь физические TXT lines без семантической группировки.

    Args:
        limits: Неизменяемые TextParserLimits; ``None`` выбирает defaults.

    Probe читает bounded sample, возвращает кодировку, confidence и warnings.
    Parse читает source через context.reader и выдаёт ExtractedBatch по lines;
    line provenance начинается с единицы. Embedded content не выполняется.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Кодировка не определена/не поддержана
            (``PARSER_ENCODING_UNSUPPORTED``) или ввод некорректен
            (``PARSER_MALFORMED_INPUT``); replacement decoding не применяется.
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.text"
    version = "1.0.0"
    priority = -100

    def __init__(self, *, limits: TextParserLimits | None = None) -> None:
        self._limits = limits or TextParserLimits()
        if type(self._limits) is not TextParserLimits:
            raise ValueError("limits должен быть экземпляром TextParserLimits")

    @property
    def limits(self) -> TextParserLimits:
        """Вернуть immutable instance-local resource limits."""

        return self._limits

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Подтвердить bounded decodable text, не доверяя MIME и extension."""

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
        supported = is_text_like(text)
        content_signal = ProbeSignal(
            kind=ProbeSignalKind.CONTENT_MEDIA_TYPE,
            outcome=(
                ProbeSignalOutcome.MATCH if supported else ProbeSignalOutcome.MISMATCH
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
            detected_media_type="text/plain" if supported else None,
            detected_encoding=detection.reported_encoding,
            warnings=detection.warnings,
            format_id="txt" if supported else None,
            signals=(content_signal, *advisory),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть ordered physical lines без paragraph/schema inference."""

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

    async def _units(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[PhysicalUnit]:
        budget = UnitBudget.from_context(self.adapter_id, context)
        async for line in iter_physical_lines(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
        ):
            budget.ensure_pending(record_count=1, physical_count=1)
            unit = PhysicalUnit(
                lines=(extracted_line(source, line),),
                blocks=(),
                record_count=1,
                is_source_last=line.is_source_last,
            )
            budget.commit(record_count=1, physical_count=1)
            yield unit


__all__ = ("PlainTextParser", "TextParserLimits")
