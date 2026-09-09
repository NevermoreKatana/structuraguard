"""Technical adapter монолитного JSON document."""

from __future__ import annotations

from collections.abc import AsyncIterator

from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.ports.source import ParseContext, ProbeContext

from ._json import (
    JsonParserLimits,
    probe_json_document,
    stream_json_document,
)


class JsonDocumentParser:
    """Извлечь JSON object, array или scalar без flatten и semantic naming.

    Args:
        limits: Неизменяемые JsonParserLimits; ``None`` выбирает defaults.

    Probe проверяет bounded content. Parse использует reader/лимиты context,
    возвращает ExtractedBatch с деревом и JSON Pointer provenance. Duplicate
    keys различаются occurrence_path, числа сохраняются как lexical tokens.
    Только UTF-8 с optional BOM; строки и ключи никогда не исполняются.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Некорректен JSON (``PARSER_MALFORMED_INPUT``) или UTF-8
            (``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.json"
    version = "1.0.0"
    priority = 40

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
        """Подтвердить один strict JSON document по bounded content."""

        return await probe_json_document(
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
        """Потоково выдать complete root/item subtrees без destructive flatten."""

        return stream_json_document(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
            version=self.version,
        )


__all__ = ("JsonDocumentParser",)
