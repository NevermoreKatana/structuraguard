"""Technical adapter JSON Lines и NDJSON aliases."""

from __future__ import annotations

from collections.abc import AsyncIterator

from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.ports.source import ParseContext, ProbeContext

from ._json import JsonParserLimits, probe_json_lines, stream_json_lines


class JsonLinesParser:
    """Извлечь независимые JSONL/NDJSON trees потоковыми batches.

    Args:
        limits: Неизменяемые JsonParserLimits; ``None`` выбирает defaults.

    Probe требует минимум две полные записи. Parse использует reader/лимиты
    context и выдаёт ExtractedBatch между целыми records, включая scalar roots.
    Пустые строки не считаются records, но сохраняют physical line numbering.
    Только UTF-8 с optional BOM; raw values инертны, semantic naming отсутствует.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Некорректен JSON (``PARSER_MALFORMED_INPUT`` с точным
            one-based line_number) или UTF-8 (``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.json-lines"
    version = "1.0.0"
    priority = 50

    def __init__(self, *, limits: JsonParserLimits | None = None) -> None:
        self._limits = JsonParserLimits() if limits is None else limits
        if type(self._limits) is not JsonParserLimits:
            raise ValueError("limits должен быть JsonParserLimits")

    @property
    def limits(self) -> JsonParserLimits:
        """Вернуть immutable instance-local resource limits."""

        return self._limits

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Подтвердить минимум две complete JSON records на физических строках."""

        return await probe_json_lines(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
            version=self.version,
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Читать JSONL/NDJSON строго по строкам и complete record boundaries."""

        return stream_json_lines(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
            version=self.version,
        )


__all__ = ("JsonLinesParser",)
