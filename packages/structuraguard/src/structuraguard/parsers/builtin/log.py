"""Отдельный adapter для фиксированных physical log formats."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedValue,
    LineRangeLocation,
    PhysicalMetadataEntry,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import (
    LogParserLimits,
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

_MEDIA_TYPES = frozenset({"text/x-log", "text/log", "application/x-log", "text/plain"})
_EXTENSIONS = frozenset({".log"})
_LEVELS = r"TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|CRITICAL|FATAL|ALERT|EMERG"
_ISO_EVENT = re.compile(
    rf"^(?P<timestamp>[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}[ T]"
    rf"[0-9]{{2}}:[0-9]{{2}}:[0-9]{{2}}"
    rf"(?:\.[0-9]{{1,9}})?(?:Z|[+-][0-9]{{2}}:[0-9]{{2}})?)"
    rf"[ \t]+(?P<level>{_LEVELS})(?=[ \t:|\]-]|$)"
)
_APACHE_COMBINED = re.compile(
    r"^(?P<host>\S{1,255}) \S{1,255} \S{1,255} "
    r"\[(?P<timestamp>[^\]\r\n]{1,128})\] "
    r'"(?P<request>[^"\r\n]{1,4096})" '
    r"(?P<status>[0-9]{3}) (?P<bytes>[0-9]{1,32}|-)"
    r'(?: "[^"\r\n]{0,4096}" "[^"\r\n]{0,4096}")?$'
)
_KEY_VALUE = re.compile(
    r"(?<!\S)(?P<key>[A-Za-z_][A-Za-z0-9_.-]{0,63})="
    r"(?P<value>[^\s=]{1,1024})(?=$|\s)"
)
_MARKDOWN_FENCE_START = re.compile(r"^ {0,3}(?:`{3,}|~{3,})(?:[^`~].*)?$")
_KNOWN_KV_STRUCTURE_KEYS = frozenset({"level", "severity", "time", "timestamp", "ts"})
_MAX_PROBE_LINES = 256


def _reject_json_constant(_value: str) -> None:
    raise ValueError("Non-finite JSON constant запрещён")


@dataclass(frozen=True, slots=True)
class _Capture:
    hint: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Recognition:
    name: str
    captures: tuple[_Capture, ...]
    capture_overflow: bool = False


def _capture_limit_error(limit: int) -> SecurityPolicyError:
    return limit_error(
        adapter_id="builtin.log",
        resource="capture_groups",
        limit=limit,
    )


def _bounded_key_value_captures(
    line: str,
    *,
    start: int = 0,
    initial: tuple[_Capture, ...] = (),
    limit: int,
    require_leading_token: bool = False,
    fail_on_overflow: bool = True,
) -> tuple[tuple[_Capture, ...], frozenset[str], bool]:
    captures = list(initial)
    keys: set[str] = set()
    overflow = False
    for match in _KEY_VALUE.finditer(line, start):
        if (
            require_leading_token
            and not captures
            and line[start : match.start()].strip(" \t")
        ):
            return (), frozenset(), False
        if len(captures) >= limit:
            if fail_on_overflow:
                raise _capture_limit_error(limit)
            overflow = True
        else:
            captures.append(_Capture("key_value", *match.span(0)))
        keys.add(match.group("key").casefold())
    return tuple(captures), frozenset(keys), overflow


def _recognize(
    line: str,
    *,
    max_capture_groups: int,
    enforce_capture_limit: bool = True,
) -> _Recognition | None:
    iso = _ISO_EVENT.match(line)
    if iso is not None:
        iso_captures = (
            _Capture("timestamp", *iso.span("timestamp")),
            _Capture("log_level", *iso.span("level")),
        )
        fixed_overflow = len(iso_captures) > max_capture_groups
        if fixed_overflow and enforce_capture_limit:
            raise _capture_limit_error(max_capture_groups)
        bounded, _, kv_overflow = _bounded_key_value_captures(
            line,
            start=iso.end(),
            initial=iso_captures[:max_capture_groups],
            limit=max_capture_groups,
            fail_on_overflow=enforce_capture_limit,
        )
        return _Recognition("iso", bounded, fixed_overflow or kv_overflow)

    apache = _APACHE_COMBINED.fullmatch(line)
    if apache is not None:
        apache_captures = (
            _Capture("host", *apache.span("host")),
            _Capture("timestamp", *apache.span("timestamp")),
            _Capture("http_request", *apache.span("request")),
            _Capture("http_status", *apache.span("status")),
            _Capture("byte_count", *apache.span("bytes")),
        )
        overflow = len(apache_captures) > max_capture_groups
        if overflow and enforce_capture_limit:
            raise _capture_limit_error(max_capture_groups)
        return _Recognition(
            "apache_combined",
            apache_captures[:max_capture_groups],
            overflow,
        )

    kv_captures, keys, overflow = _bounded_key_value_captures(
        line,
        limit=max_capture_groups,
        require_leading_token=True,
        fail_on_overflow=False,
    )
    has_known_structure = bool(keys & _KNOWN_KV_STRUCTURE_KEYS)
    if overflow and has_known_structure:
        if enforce_capture_limit:
            raise _capture_limit_error(max_capture_groups)
        return _Recognition("key_value", kv_captures, True)
    if len(kv_captures) >= 2 and has_known_structure:
        return _Recognition("key_value", kv_captures)
    return None


def _is_json_lines(lines: tuple[str, ...]) -> bool:
    candidates = tuple(line for line in lines if line.strip())
    if len(candidates) < 2 or any(
        not line.lstrip().startswith("{") for line in candidates
    ):
        return False
    for line in candidates:
        try:
            value = json.loads(
                line,
                parse_int=str,
                parse_float=str,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, RecursionError):
            return False
        if type(value) is not dict:
            return False
    return True


def _has_repeated_structure(
    lines: tuple[str, ...],
    *,
    max_capture_groups: int,
) -> bool:
    if any(_MARKDOWN_FENCE_START.match(line) is not None for line in lines):
        return False
    if _is_json_lines(lines):
        return False
    recognizers: Counter[str] = Counter()
    overflowed: set[str] = set()
    for line in lines:
        if not line.strip():
            continue
        recognition = _recognize(
            line,
            max_capture_groups=max_capture_groups,
            enforce_capture_limit=False,
        )
        if recognition is None:
            continue
        recognizers[recognition.name] += 1
        if recognition.capture_overflow:
            overflowed.add(recognition.name)
        if recognizers[recognition.name] >= 2 and recognition.name in overflowed:
            raise _capture_limit_error(max_capture_groups)
    return bool(recognizers) and max(recognizers.values()) >= 2


class LogParser:
    """Извлечь LOG lines/events только с фиксированными technical recognizers.

    Args:
        limits: Неизменяемые LogParserLimits; ``None`` выбирает defaults.

    Probe требует повторяемой известной структуры, а не одного regex match.
    Parse использует reader/лимиты context, сохраняет lines, event blocks и raw
    hints в ExtractedBatch; неизвестные строки не получают угаданную схему.
    Caller regex и выполнение embedded content не поддерживаются.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Ошибка кодировки (``PARSER_ENCODING_UNSUPPORTED``) или ввода
            (``PARSER_MALFORMED_INPUT``), без replacement decoding.
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.log"
    version = "1.0.0"
    priority = 20

    def __init__(self, *, limits: LogParserLimits | None = None) -> None:
        self._limits = limits or LogParserLimits()
        if type(self._limits) is not LogParserLimits:
            raise ValueError("limits должен быть экземпляром LogParserLimits")

    @property
    def limits(self) -> LogParserLimits:
        """Вернуть immutable instance-local resource limits."""

        return self._limits

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Подтвердить только повторяемую известную physical log structure."""

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
        supported = is_text_like(text) and _has_repeated_structure(
            lines,
            max_capture_groups=self._limits.max_capture_groups,
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
            detected_media_type="text/x-log" if supported else None,
            detected_encoding=detection.reported_encoding,
            warnings=detection.warnings,
            format_id="log" if supported else None,
            signals=(structure, *advisory),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Извлечь raw lines/events и bounded deterministic hints."""

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

    def _event_unit(
        self,
        source: SourceArtifact,
        event_lines: tuple[PhysicalLine, ...],
        recognition: _Recognition | None,
        *,
        block_order: int,
        first_value_number: int,
        is_source_last: bool,
        budget: UnitBudget,
    ) -> tuple[PhysicalUnit, int]:
        capture_count = len(recognition.captures) if recognition is not None else 0
        physical_count = len(event_lines) + 1 + capture_count
        budget.ensure_pending(record_count=1, physical_count=physical_count)
        first_line = event_lines[0]
        last_line = event_lines[-1]
        values: list[ExtractedValue] = []
        next_value_number = first_value_number
        if recognition is not None:
            for capture in recognition.captures:
                values.append(
                    ExtractedValue(
                        value_id=f"value-{next_value_number}",
                        raw_value=StringScalar(
                            value=first_line.text[capture.start : capture.end]
                        ),
                        location=LineRangeLocation(
                            source=source.ref,
                            line_start=first_line.number,
                            line_end=first_line.number,
                            column_start=capture.start,
                            column_end=capture.end,
                        ),
                        technical_type_hint=capture.hint,
                    )
                )
                next_value_number += 1

        block = ExtractedBlock(
            block_id=f"block-{block_order + 1}",
            kind=ExtractedBlockKind.LINE,
            order=block_order,
            location=LineRangeLocation(
                source=source.ref,
                line_start=first_line.number,
                line_end=last_line.number,
            ),
            text="".join(line.raw_text for line in event_lines),
            values=tuple(values),
            metadata=(
                PhysicalMetadataEntry(key="physical_kind", value="log_event"),
                PhysicalMetadataEntry(
                    key="recognizer",
                    value=recognition.name
                    if recognition is not None
                    else "unrecognized",
                ),
            ),
        )
        unit = PhysicalUnit(
            lines=tuple(extracted_line(source, line) for line in event_lines),
            blocks=(block,),
            record_count=1,
            is_source_last=is_source_last,
        )
        budget.commit(record_count=1, physical_count=physical_count)
        return (
            unit,
            next_value_number,
        )

    async def _units(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[PhysicalUnit]:
        current: list[PhysicalLine] = []
        current_chars = 0
        current_recognition: _Recognition | None = None
        block_order = 0
        value_number = 1
        budget = UnitBudget.from_context(self.adapter_id, context)

        async for line in iter_physical_lines(
            source,
            context,
            self._limits,
            adapter_id=self.adapter_id,
        ):
            recognition = _recognize(
                line.text,
                max_capture_groups=self._limits.max_capture_groups,
            )
            if recognition is not None and current:
                unit, value_number = self._event_unit(
                    source,
                    tuple(current),
                    current_recognition,
                    block_order=block_order,
                    first_value_number=value_number,
                    is_source_last=False,
                    budget=budget,
                )
                yield unit
                block_order += 1
                current.clear()
                current_chars = 0
                current_recognition = None

            pending_recognition = recognition if not current else current_recognition
            pending_capture_count = (
                len(pending_recognition.captures)
                if pending_recognition is not None
                else 0
            )
            budget.ensure_pending(
                record_count=1,
                physical_count=len(current) + 2 + pending_capture_count,
            )

            next_line_count = len(current) + 1
            if next_line_count > self._limits.max_event_lines:
                raise limit_error(
                    adapter_id=self.adapter_id,
                    resource="event_lines",
                    limit=self._limits.max_event_lines,
                )
            next_chars = current_chars + len(line.raw_text)
            if next_chars > self._limits.max_event_chars:
                raise limit_error(
                    adapter_id=self.adapter_id,
                    resource="event_chars",
                    limit=self._limits.max_event_chars,
                )
            current.append(line)
            current_chars = next_chars
            if len(current) == 1:
                current_recognition = recognition

            if line.is_source_last:
                unit, value_number = self._event_unit(
                    source,
                    tuple(current),
                    current_recognition,
                    block_order=block_order,
                    first_value_number=value_number,
                    is_source_last=True,
                    budget=budget,
                )
                del value_number
                yield unit
                return

        if current:
            unit, value_number = self._event_unit(
                source,
                tuple(current),
                current_recognition,
                block_order=block_order,
                first_value_number=value_number,
                is_source_last=True,
                budget=budget,
            )
            del value_number
            yield unit


__all__ = ("LogParser", "LogParserLimits")
