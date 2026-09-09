"""Общие bounded-примитивы узких text parser adapters."""

from __future__ import annotations

import asyncio
import codecs
import unicodedata
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from io import StringIO
from typing import Final, cast

from charset_normalizer import from_bytes

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.common import (
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedBlock,
    ExtractedDatasetManifest,
    ExtractedLine,
    ExtractedSourceIndex,
    LineRangeLocation,
    PhysicalMetadataEntry,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers._hashing import batch_fingerprint, manifest_fingerprint
from structuraguard.ports.source import ParseContext, ProbeContext, SourceReader

_SCHEMA_VERSION: Final = "1.1.0"
_SDK_VERSION: Final = "0.3.0"
_PLACEHOLDER_FINGERPRINT: Final = "sha256:" + "0" * 64
_MAX_LINE_CHARS: Final = 16 * 1024 * 1024
_MAX_LINES: Final = 10_000_000
_MAX_READ_CHUNK_BYTES: Final = 1024 * 1024
_MAX_ENCODING_PROBE_BYTES: Final = 1024 * 1024
_MAX_BLOCK_CHARS: Final = 32 * 1024 * 1024
_MAX_EVENT_LINES: Final = 100_000
_MAX_CAPTURE_GROUPS: Final = 1_024
_MIN_ENCODING_PREFIX_BYTES: Final = 4
_MAX_INDEXED_REFS: Final = 10_000

_SAFE_DETECTED_ENCODINGS: Final = frozenset(
    {
        "ascii",
        "big5",
        "cp037",
        "cp424",
        "cp437",
        "cp500",
        "cp720",
        "cp737",
        "cp775",
        "cp850",
        "cp852",
        "cp855",
        "cp856",
        "cp857",
        "cp858",
        "cp860",
        "cp861",
        "cp862",
        "cp863",
        "cp864",
        "cp865",
        "cp866",
        "cp869",
        "cp874",
        "cp932",
        "cp949",
        "cp950",
        "cp1250",
        "cp1251",
        "cp1252",
        "cp1253",
        "cp1254",
        "cp1255",
        "cp1256",
        "cp1257",
        "cp1258",
        "euc-jp",
        "euc-kr",
        "gb18030",
        "gb2312",
        "gbk",
        "iso8859-1",
        "iso8859-2",
        "iso8859-3",
        "iso8859-4",
        "iso8859-5",
        "iso8859-6",
        "iso8859-7",
        "iso8859-8",
        "iso8859-9",
        "iso8859-10",
        "iso8859-11",
        "iso8859-13",
        "iso8859-14",
        "iso8859-15",
        "iso8859-16",
        "johab",
        "koi8-r",
        "koi8-t",
        "koi8-u",
        "mac-cyrillic",
        "mac-greek",
        "mac-iceland",
        "mac-latin2",
        "mac-roman",
        "mac-turkish",
        "shift-jis",
        "utf-8",
        "utf-8-sig",
        "utf-16-le",
        "utf-16-be",
        "utf-32-le",
        "utf-32-be",
    }
)


def _positive_int(value: int, *, field_name: str, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(
            f"{field_name} должен быть положительным int не больше {maximum}"
        )


def _confidence(value: Decimal, *, field_name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or not 0 <= value <= 1:
        raise ValueError(
            f"{field_name} должен быть конечным Decimal в диапазоне [0, 1]"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class TextParserLimits:
    """Instance-local пределы декодирования и physical lines."""

    max_line_chars: int = 1_000_000
    max_lines: int = 1_000_000
    read_chunk_bytes: int = 64 * 1024
    encoding_probe_bytes: int = 64 * 1024
    min_encoding_confidence: Decimal = Decimal("0.50")
    low_encoding_confidence: Decimal = Decimal("0.80")

    def __post_init__(self) -> None:
        _positive_int(
            self.max_line_chars,
            field_name="max_line_chars",
            maximum=_MAX_LINE_CHARS,
        )
        _positive_int(self.max_lines, field_name="max_lines", maximum=_MAX_LINES)
        _positive_int(
            self.read_chunk_bytes,
            field_name="read_chunk_bytes",
            maximum=_MAX_READ_CHUNK_BYTES,
        )
        _positive_int(
            self.encoding_probe_bytes,
            field_name="encoding_probe_bytes",
            maximum=_MAX_ENCODING_PROBE_BYTES,
        )
        _confidence(
            self.min_encoding_confidence,
            field_name="min_encoding_confidence",
        )
        _confidence(
            self.low_encoding_confidence,
            field_name="low_encoding_confidence",
        )
        if self.low_encoding_confidence < self.min_encoding_confidence:
            raise ValueError(
                "low_encoding_confidence не может быть меньше min_encoding_confidence"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class LogParserLimits(TextParserLimits):
    """Пределы physical log event поверх общих text limits."""

    max_event_lines: int = 1_000
    max_event_chars: int = 4_000_000
    max_capture_groups: int = 64

    def __post_init__(self) -> None:
        TextParserLimits.__post_init__(self)
        _positive_int(
            self.max_event_lines,
            field_name="max_event_lines",
            maximum=_MAX_EVENT_LINES,
        )
        _positive_int(
            self.max_event_chars,
            field_name="max_event_chars",
            maximum=_MAX_BLOCK_CHARS,
        )
        _positive_int(
            self.max_capture_groups,
            field_name="max_capture_groups",
            maximum=_MAX_CAPTURE_GROUPS,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MarkdownParserLimits(TextParserLimits):
    """Пределы physical Markdown block поверх общих text limits."""

    max_block_chars: int = 4_000_000

    def __post_init__(self) -> None:
        TextParserLimits.__post_init__(self)
        _positive_int(
            self.max_block_chars,
            field_name="max_block_chars",
            maximum=_MAX_BLOCK_CHARS,
        )


@dataclass(frozen=True, slots=True)
class EncodingDetection:
    """Bounded результат выбора безопасного incremental codec."""

    reported_encoding: str
    codec: str
    confidence: Decimal
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PhysicalLine:
    """Декодированная строка с сохранённым physical terminator."""

    number: int
    text: str
    terminator: str
    is_source_last: bool

    @property
    def raw_text(self) -> str:
        """Вернуть exact decoded span вместе с исходным terminator."""

        return f"{self.text}{self.terminator}"


@dataclass(frozen=True, slots=True)
class PhysicalUnit:
    """Неделимая line/block/event единица batching."""

    lines: tuple[ExtractedLine, ...]
    blocks: tuple[ExtractedBlock, ...]
    record_count: int
    is_source_last: bool


@dataclass(slots=True)
class UnitBudget:
    """Cumulative adapter-side budget до materialization цельной unit."""

    adapter_id: str
    max_records: int
    max_physical_objects: int
    committed_records: int = 0
    committed_physical_objects: int = 0

    @classmethod
    def from_context(cls, adapter_id: str, context: ParseContext) -> UnitBudget:
        """Создать budget из validated ParseContext."""

        return cls(
            adapter_id=adapter_id,
            max_records=context.max_records,
            max_physical_objects=context.max_physical_objects,
        )

    def ensure_pending(self, *, record_count: int, physical_count: int) -> None:
        """Проверить prospective unit до добавления raw physical state."""

        if record_count > self.max_records - self.committed_records:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="records",
                limit=self.max_records,
            )
        if physical_count > (
            self.max_physical_objects - self.committed_physical_objects
        ):
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="physical_objects",
                limit=self.max_physical_objects,
            )

    def commit(self, *, record_count: int, physical_count: int) -> None:
        """Зафиксировать уже построенную и готовую к yield unit."""

        self.ensure_pending(
            record_count=record_count,
            physical_count=physical_count,
        )
        self.committed_records += record_count
        self.committed_physical_objects += physical_count


def _parser_error(reason: str) -> ParserError:
    return ParserError(
        error_code="PARSER_MALFORMED_INPUT",
        message="Source snapshot нарушает bounded parser input contract.",
        details={"reason": reason},
    )


def encoding_error(reason: str, *, encoding: str | None = None) -> ParserError:
    """Создать typed encoding error без включения raw source bytes."""

    details = {"reason": reason}
    if encoding is not None:
        details["encoding"] = encoding
    return ParserError(
        error_code="PARSER_ENCODING_UNSUPPORTED",
        message="Кодировка source не может быть безопасно определена или декодирована.",
        details=details,
    )


def limit_error(*, adapter_id: str, resource: str, limit: int) -> SecurityPolicyError:
    """Создать одинаковый sanitized outcome для resource limit."""

    return SecurityPolicyError(
        error_code="SECURITY_LIMIT_EXCEEDED",
        message="Technical parser превысил настроенный resource limit.",
        details={
            "adapter_id": adapter_id,
            "limit": limit,
            "resource": resource,
        },
    )


async def _read_checked(
    reader: SourceReader,
    *,
    offset: int,
    size: int,
) -> bytes:
    returned = await reader.read(offset=offset, size=size)
    if type(returned) is not bytes:
        raise _parser_error("reader_result_type")
    if len(returned) > size:
        raise _parser_error("reader_result_size")
    return returned


async def read_probe_sample(
    source: SourceArtifact,
    context: ProbeContext,
    limits: TextParserLimits,
) -> bytes:
    """Прочитать только bounded head snapshot, корректно учитывая short reads."""

    target = min(
        source.size_bytes,
        context.max_probe_bytes,
        limits.encoding_probe_bytes,
    )
    if target == 0:
        return b""
    sample = bytearray()
    offset = 0
    while offset < target:
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, target - offset),
        )
        if not chunk:
            raise _parser_error("unexpected_eof")
        sample.extend(chunk)
        offset += len(chunk)
        await asyncio.sleep(0)
    return bytes(sample)


async def _read_initial_sample(
    source: SourceArtifact,
    context: ParseContext,
    limits: TextParserLimits,
) -> bytes:
    target = min(source.size_bytes, limits.encoding_probe_bytes)
    if target == 0:
        return b""

    initial_read_size = min(limits.read_chunk_bytes, target)
    if context.detected_encoding is not None:
        initial_read_size = min(initial_read_size, _MIN_ENCODING_PREFIX_BYTES)
    first = await _read_checked(
        context.reader,
        offset=0,
        size=initial_read_size,
    )
    if not first:
        raise _parser_error("unexpected_eof")
    sample_buffer = bytearray(first)
    offset = len(first)

    while offset < min(target, _MIN_ENCODING_PREFIX_BYTES):
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, target - offset),
        )
        if not chunk:
            raise _parser_error("unexpected_eof")
        sample_buffer.extend(chunk)
        offset += len(chunk)

    if context.detected_encoding is not None:
        return bytes(sample_buffer)

    while offset < target:
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, target - offset),
        )
        if not chunk:
            raise _parser_error("unexpected_eof")
        sample_buffer.extend(chunk)
        offset += len(chunk)
        await asyncio.sleep(0)
    return bytes(sample_buffer)


def _bom_detection(sample: bytes) -> EncodingDetection | None:
    # UTF-32 BOM обязан проверяться до его UTF-16 prefix.
    candidates = (
        (codecs.BOM_UTF32_LE, "utf-32-le", "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32-be", "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig", "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16"),
    )
    for marker, reported, codec in candidates:
        if sample.startswith(marker):
            return EncodingDetection(reported, codec, Decimal("1"), ())
    return None


def _canonical_codec(value: str) -> str | None:
    try:
        canonical = codecs.lookup(value).name.replace("_", "-")
    except LookupError:
        return None
    if canonical not in _SAFE_DETECTED_ENCODINGS:
        return None
    return canonical


def _context_encoding_detection(value: str, *, sample: bytes) -> EncodingDetection:
    codec = _canonical_codec(value)
    if codec is None:
        raise encoding_error("unsupported_codec")
    bom_codec = {
        "utf-16-le": (codecs.BOM_UTF16_LE, "utf-16"),
        "utf-16-be": (codecs.BOM_UTF16_BE, "utf-16"),
        "utf-32-le": (codecs.BOM_UTF32_LE, "utf-32"),
        "utf-32-be": (codecs.BOM_UTF32_BE, "utf-32"),
    }.get(codec)
    bom_aware_codec = (
        bom_codec[1]
        if bom_codec is not None and sample.startswith(bom_codec[0])
        else codec
    )
    return EncodingDetection(codec, bom_aware_codec, Decimal("1"), ())


def detect_encoding(
    sample: bytes,
    *,
    sample_is_complete: bool,
    limits: TextParserLimits,
) -> EncodingDetection:
    """Определить codec по BOM, strict UTF-8 либо bounded heuristic sample."""

    if not sample:
        return EncodingDetection("utf-8", "utf-8", Decimal("1"), ())

    bom = _bom_detection(sample)
    if bom is not None:
        return bom

    try:
        utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        utf8_decoder.decode(sample, final=sample_is_complete)
    except UnicodeDecodeError:
        pass
    else:
        return EncodingDetection("utf-8", "utf-8", Decimal("1"), ())

    match = from_bytes(sample).best()
    if match is None or match.encoding is None:
        raise encoding_error("undetermined")
    codec = _canonical_codec(match.encoding)
    confidence = Decimal(str(match.coherence))
    if codec is None or confidence < limits.min_encoding_confidence:
        raise encoding_error("undetermined")

    try:
        detected_decoder = codecs.getincrementaldecoder(codec)(errors="strict")
        detected_decoder.decode(sample, final=sample_is_complete)
    except (LookupError, UnicodeDecodeError):
        raise encoding_error("decode_failed", encoding=codec) from None

    warnings = ["PARSER_ENCODING_HEURISTIC"]
    if confidence < limits.low_encoding_confidence:
        warnings.append("PARSER_ENCODING_LOW_CONFIDENCE")
    return EncodingDetection(codec, codec, confidence, tuple(warnings))


def has_binary_container_signature(sample: bytes) -> bool:
    """Распознать только явные signatures контейнеров, не MIME/extension hints."""

    return sample.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"%PDF-"))


def binary_container_probe(
    sample: bytes, *, source: SourceArtifact, adapter_id: str, adapter_version: str
) -> ProbeResult | None:
    """Явный ZIP/PDF signature не должен запускать text encoding heuristics."""

    if not has_binary_container_signature(sample):
        return None
    return ProbeResult(
        source=source.ref,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        supported=False,
        confidence=Decimal(0),
        signals=(
            ProbeSignal(
                kind=ProbeSignalKind.SIGNATURE, outcome=ProbeSignalOutcome.MISMATCH
            ),
        ),
    )


def decode_probe_sample(
    sample: bytes,
    *,
    source_size: int,
    limits: TextParserLimits,
) -> tuple[str, EncodingDetection]:
    """Строго декодировать bounded probe sample без замены malformed bytes."""

    detection = detect_encoding(
        sample,
        sample_is_complete=len(sample) == source_size,
        limits=limits,
    )
    try:
        decoder = codecs.getincrementaldecoder(detection.codec)(errors="strict")
        text = decoder.decode(sample, final=len(sample) == source_size)
    except (LookupError, UnicodeDecodeError):
        raise encoding_error(
            "decode_failed",
            encoding=detection.reported_encoding,
        ) from None
    return text, detection


def is_text_like(value: str) -> bool:
    """Отклонить binary spoof, не интерпретируя и не нормализуя raw text."""

    if "\x00" in value:
        return False
    if not value:
        return True
    forbidden_controls = sum(
        1
        for character in value
        if unicodedata.category(character) == "Cc"
        and character not in {"\t", "\n", "\r", "\f", "\x1b"}
    )
    return forbidden_controls * 10 <= len(value)


def advisory_signals(
    source: SourceArtifact,
    *,
    media_types: frozenset[str],
    extensions: frozenset[str],
) -> tuple[ProbeSignal, ProbeSignal]:
    """Построить только advisory MIME/extension signals."""

    declared = source.media_type.partition(";")[0].strip().casefold()
    declared_outcome = (
        ProbeSignalOutcome.MATCH
        if declared in media_types
        else ProbeSignalOutcome.MISMATCH
    )
    _, separator, suffix = source.display_name.rpartition(".")
    extension_outcome = ProbeSignalOutcome.INCONCLUSIVE
    if separator:
        extension_outcome = (
            ProbeSignalOutcome.MATCH
            if f".{suffix.casefold()}" in extensions
            else ProbeSignalOutcome.MISMATCH
        )
    return (
        ProbeSignal(
            kind=ProbeSignalKind.DECLARED_MEDIA_TYPE,
            outcome=declared_outcome,
        ),
        ProbeSignal(
            kind=ProbeSignalKind.EXTENSION,
            outcome=extension_outcome,
        ),
    )


def probe_lines(value: str, *, max_lines: int) -> tuple[str, ...]:
    """Разделить probe text только по тем же CR/LF boundaries, что и parse."""

    lines: list[str] = []
    start = 0
    position = 0
    while position < len(value) and len(lines) < max_lines:
        character = value[position]
        if character not in {"\r", "\n"}:
            position += 1
            continue
        lines.append(value[start:position])
        if (
            character == "\r"
            and position + 1 < len(value)
            and value[position + 1] == "\n"
        ):
            position += 1
        position += 1
        start = position
    if start < len(value) and len(lines) < max_lines:
        lines.append(value[start:])
    return tuple(lines)


def line_metadata(terminator: str) -> tuple[PhysicalMetadataEntry, ...]:
    """Сериализовать newline convention без изменения line text."""

    ending = {"": "none", "\n": "lf", "\r": "cr", "\r\n": "crlf"}[terminator]
    return (PhysicalMetadataEntry(key="line_ending", value=ending),)


def extracted_line(source: SourceArtifact, line: PhysicalLine) -> ExtractedLine:
    """Преобразовать physical line в lossless ESM DTO."""

    return ExtractedLine(
        line_id=f"line-{line.number}",
        line_number=line.number,
        text=line.text,
        location=LineRangeLocation(
            source=source.ref,
            line_start=line.number,
            line_end=line.number,
        ),
        metadata=line_metadata(line.terminator),
    )


async def iter_physical_lines(
    source: SourceArtifact,
    context: ParseContext,
    limits: TextParserLimits,
    *,
    adapter_id: str,
) -> AsyncIterator[PhysicalLine]:
    """Incremental strict decoder и CR/LF splitter с bounded line state."""

    if source.size_bytes > context.max_bytes:
        raise limit_error(
            adapter_id=adapter_id,
            resource="source_bytes",
            limit=context.max_bytes,
        )
    if source.size_bytes == 0:
        return

    initial = await _read_initial_sample(source, context, limits)
    detection = (
        detect_encoding(
            initial,
            sample_is_complete=len(initial) == source.size_bytes,
            limits=limits,
        )
        if context.detected_encoding is None
        else _context_encoding_detection(
            context.detected_encoding,
            sample=initial,
        )
    )
    try:
        decoder = codecs.getincrementaldecoder(detection.codec)(errors="strict")
    except LookupError:
        raise encoding_error(
            "decode_failed",
            encoding=detection.reported_encoding,
        ) from None

    line_buffer = StringIO()
    line_length = 0
    line_number = 1
    awaiting_cr = False
    offset = 0
    chunk = initial

    while True:
        offset += len(chunk)
        is_final_chunk = offset == source.size_bytes
        try:
            decoded = decoder.decode(chunk, final=is_final_chunk)
        except UnicodeDecodeError:
            raise encoding_error(
                "decode_failed",
                encoding=detection.reported_encoding,
            ) from None
        if "\x00" in decoded:
            raise _parser_error("binary_content")

        position = 0
        if awaiting_cr and decoded:
            terminator = "\r\n" if decoded.startswith("\n") else "\r"
            if line_number > limits.max_lines:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="line_count",
                    limit=limits.max_lines,
                )
            position = 1 if terminator == "\r\n" else 0
            yield PhysicalLine(
                line_number,
                line_buffer.getvalue(),
                terminator,
                is_final_chunk and position == len(decoded),
            )
            line_buffer.seek(0)
            line_buffer.truncate(0)
            line_length = 0
            line_number += 1
            awaiting_cr = False

        while position < len(decoded):
            cr_index = decoded.find("\r", position)
            lf_index = decoded.find("\n", position)
            newline_indices = tuple(
                index for index in (cr_index, lf_index) if index >= 0
            )
            if not newline_indices:
                segment = decoded[position:]
                if segment and line_number > limits.max_lines:
                    raise limit_error(
                        adapter_id=adapter_id,
                        resource="line_count",
                        limit=limits.max_lines,
                    )
                if line_length + len(segment) > limits.max_line_chars:
                    raise limit_error(
                        adapter_id=adapter_id,
                        resource="line_chars",
                        limit=limits.max_line_chars,
                    )
                if segment:
                    line_buffer.write(segment)
                    line_length += len(segment)
                break

            newline_index = min(newline_indices)
            segment = decoded[position:newline_index]
            if segment and line_number > limits.max_lines:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="line_count",
                    limit=limits.max_lines,
                )
            if line_length + len(segment) > limits.max_line_chars:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="line_chars",
                    limit=limits.max_line_chars,
                )
            if segment:
                line_buffer.write(segment)
                line_length += len(segment)

            character = decoded[newline_index]
            if character == "\r" and newline_index == len(decoded) - 1:
                awaiting_cr = True
                position = len(decoded)
                break
            if character == "\r" and decoded[newline_index + 1] == "\n":
                terminator = "\r\n"
                next_position = newline_index + 2
            else:
                terminator = character
                next_position = newline_index + 1

            if line_number > limits.max_lines:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="line_count",
                    limit=limits.max_lines,
                )
            yield PhysicalLine(
                line_number,
                line_buffer.getvalue(),
                terminator,
                is_final_chunk and next_position == len(decoded),
            )
            line_buffer.seek(0)
            line_buffer.truncate(0)
            line_length = 0
            line_number += 1
            position = next_position

        if is_final_chunk:
            if awaiting_cr:
                if line_number > limits.max_lines:
                    raise limit_error(
                        adapter_id=adapter_id,
                        resource="line_count",
                        limit=limits.max_lines,
                    )
                yield PhysicalLine(
                    line_number,
                    line_buffer.getvalue(),
                    "\r",
                    True,
                )
            elif line_length:
                if line_number > limits.max_lines:
                    raise limit_error(
                        adapter_id=adapter_id,
                        resource="line_count",
                        limit=limits.max_lines,
                    )
                yield PhysicalLine(
                    line_number,
                    line_buffer.getvalue(),
                    "",
                    True,
                )
            return

        if line_number > limits.max_lines and not awaiting_cr:
            raise limit_error(
                adapter_id=adapter_id,
                resource="line_count",
                limit=limits.max_lines,
            )

        await asyncio.sleep(0)
        requested = min(
            1 if awaiting_cr else limits.read_chunk_bytes,
            source.size_bytes - offset,
        )
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=requested,
        )
        if not chunk:
            raise _parser_error("unexpected_eof")


def _parser_options_fingerprint(
    limits: TextParserLimits,
    context: ParseContext,
) -> str:
    payload = cast(
        CanonicalInput,
        {
            "batch_options": asdict(context.batch_options),
            "detected_encoding": context.detected_encoding,
            "limits": asdict(limits),
        },
    )
    return canonical_sha256_value(payload)


def extraction_id(
    source: SourceArtifact,
    *,
    adapter_id: str,
    version: str,
    options_fingerprint: str,
) -> str:
    """Получить deterministic parser-generated UUID для extraction run."""

    seed = f"{source.source_fingerprint}:{adapter_id}:{version}:{options_fingerprint}"
    return f"extraction-{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def _physical_ref_count(
    lines: tuple[ExtractedLine, ...],
    blocks: tuple[ExtractedBlock, ...],
) -> int:
    line_values = sum(len(line.values) for line in lines)
    block_lines = sum(len(block.lines) for block in blocks)
    block_line_values = sum(
        len(line.values) for block in blocks for line in block.lines
    )
    block_values = sum(len(block.values) for block in blocks)
    return (
        len(lines)
        + line_values
        + len(blocks)
        + block_lines
        + block_line_values
        + block_values
    )


def _bounded_indexed_refs(
    *,
    run_id: str,
    batch_index: int,
    lines: tuple[ExtractedLine, ...],
    blocks: tuple[ExtractedBlock, ...],
    limit: int,
) -> tuple[PhysicalSourceRef, ...]:
    """Выбрать deterministic bounded prefix адресуемых Group A objects."""

    refs: list[PhysicalSourceRef] = []

    def include(kind: PhysicalObjectKind, local_id: str | None) -> bool:
        if local_id is None:
            return True
        if len(refs) >= limit:
            return False
        refs.append(
            PhysicalSourceRef(
                extraction_id=run_id,
                batch_index=batch_index,
                kind=kind,
                local_id=local_id,
            )
        )
        return True

    all_lines = (*lines, *(line for block in blocks for line in block.lines))
    for line in all_lines:
        if not include(PhysicalObjectKind.LINE, line.line_id):
            return tuple(refs)
    for block in blocks:
        if not include(PhysicalObjectKind.BLOCK, block.block_id):
            return tuple(refs)
    for line in all_lines:
        for value in line.values:
            if not include(PhysicalObjectKind.VALUE, value.value_id):
                return tuple(refs)
    for block in blocks:
        for value in block.values:
            if not include(PhysicalObjectKind.VALUE, value.value_id):
                return tuple(refs)
    return tuple(refs)


def _build_batch(
    *,
    source: SourceArtifact,
    run_id: str,
    adapter_id: str,
    version: str,
    batch_index: int,
    lines: tuple[ExtractedLine, ...],
    blocks: tuple[ExtractedBlock, ...],
    record_count: int,
    prior_summaries: tuple[ExtractedBatchSummary, ...],
    prior_indexed_refs: Sequence[PhysicalSourceRef],
    options_fingerprint: str,
    is_last: bool,
) -> ExtractedBatch:
    indexed_refs = _bounded_indexed_refs(
        run_id=run_id,
        batch_index=batch_index,
        lines=lines,
        blocks=blocks,
        limit=_MAX_INDEXED_REFS - len(prior_indexed_refs),
    )
    draft = ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=run_id,
        batch_index=batch_index,
        source=source.ref,
        parser_id=adapter_id,
        parser_version=version,
        batch_fingerprint=_PLACEHOLDER_FINGERPRINT,
        lines=lines,
        blocks=blocks,
        record_count=record_count,
        indexed_refs=indexed_refs,
    )
    fingerprint = batch_fingerprint(draft.model_copy(update={"is_last": is_last}))
    summary = draft.model_copy(update={"batch_fingerprint": fingerprint}).to_summary()
    manifest = None
    if is_last:
        summaries = (*prior_summaries, summary)
        manifest_seed = ExtractedDatasetManifest(
            schema_version=_SCHEMA_VERSION,
            source=source.ref,
            extraction_id=run_id,
            parser_id=adapter_id,
            parser_version=version,
            producer=ProducerMetadata(
                component_id=adapter_id,
                component_version=version,
                sdk_version=_SDK_VERSION,
            ),
            parser_options_fingerprint=options_fingerprint,
            batches=summaries,
            extraction_fingerprint=_PLACEHOLDER_FINGERPRINT,
            source_index=ExtractedSourceIndex(
                refs=(*prior_indexed_refs, *indexed_refs),
            ),
            record_count=sum(item.record_count or 0 for item in summaries),
        )
        manifest = manifest_seed.model_copy(
            update={
                "extraction_fingerprint": manifest_fingerprint(manifest_seed),
            }
        )
    return ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=run_id,
        batch_index=batch_index,
        source=source.ref,
        parser_id=adapter_id,
        parser_version=version,
        batch_fingerprint=fingerprint,
        lines=lines,
        blocks=blocks,
        record_count=record_count,
        indexed_refs=indexed_refs,
        is_last=is_last,
        manifest=manifest,
    )


def _unit_physical_count(unit: PhysicalUnit) -> int:
    return _physical_ref_count(unit.lines, unit.blocks)


async def batch_units(
    units: AsyncIterator[PhysicalUnit],
    source: SourceArtifact,
    context: ParseContext,
    *,
    adapter_id: str,
    version: str,
    parser_limits: TextParserLimits,
) -> AsyncIterator[ExtractedBatch]:
    """Сформировать bounded batches, сохраняя physical unit целиком."""

    options_fingerprint = _parser_options_fingerprint(parser_limits, context)
    run_id = extraction_id(
        source,
        adapter_id=adapter_id,
        version=version,
        options_fingerprint=options_fingerprint,
    )
    summaries: list[ExtractedBatchSummary] = []
    indexed_refs: list[PhysicalSourceRef] = []
    pending_lines: list[ExtractedLine] = []
    pending_blocks: list[ExtractedBlock] = []
    pending_records = 0
    total_records = 0
    total_physical = 0
    batch_index = 0

    async for unit in units:
        next_total_records = total_records + unit.record_count
        if next_total_records > context.max_records:
            raise limit_error(
                adapter_id=adapter_id,
                resource="records",
                limit=context.max_records,
            )
        next_total_physical = total_physical + _unit_physical_count(unit)
        if next_total_physical > context.max_physical_objects:
            raise limit_error(
                adapter_id=adapter_id,
                resource="physical_objects",
                limit=context.max_physical_objects,
            )
        pending_lines.extend(unit.lines)
        pending_blocks.extend(unit.blocks)
        pending_records += unit.record_count
        total_records = next_total_records
        total_physical = next_total_physical

        batch_full = pending_records >= context.batch_options.batch_size
        if not batch_full and not unit.is_source_last:
            continue

        if batch_index >= context.batch_options.max_batches:
            raise limit_error(
                adapter_id=adapter_id,
                resource="batch_count",
                limit=context.batch_options.max_batches,
            )
        if (
            not unit.is_source_last
            and batch_index + 1 >= context.batch_options.max_batches
        ):
            raise limit_error(
                adapter_id=adapter_id,
                resource="batch_count",
                limit=context.batch_options.max_batches,
            )

        batch = _build_batch(
            source=source,
            run_id=run_id,
            adapter_id=adapter_id,
            version=version,
            batch_index=batch_index,
            lines=tuple(pending_lines),
            blocks=tuple(pending_blocks),
            record_count=pending_records,
            prior_summaries=tuple(summaries),
            prior_indexed_refs=indexed_refs,
            options_fingerprint=options_fingerprint,
            is_last=unit.is_source_last,
        )
        if not unit.is_source_last:
            summaries.append(batch.to_summary())
            indexed_refs.extend(batch.indexed_refs)
        yield batch
        if unit.is_source_last:
            return

        pending_lines.clear()
        pending_blocks.clear()
        pending_records = 0
        batch_index += 1
        await asyncio.sleep(0)

    if pending_records:
        if batch_index >= context.batch_options.max_batches:
            raise limit_error(
                adapter_id=adapter_id,
                resource="batch_count",
                limit=context.batch_options.max_batches,
            )
        yield _build_batch(
            source=source,
            run_id=run_id,
            adapter_id=adapter_id,
            version=version,
            batch_index=batch_index,
            lines=tuple(pending_lines),
            blocks=tuple(pending_blocks),
            record_count=pending_records,
            prior_summaries=tuple(summaries),
            prior_indexed_refs=indexed_refs,
            options_fingerprint=options_fingerprint,
            is_last=True,
        )
        return

    if batch_index >= context.batch_options.max_batches:
        raise limit_error(
            adapter_id=adapter_id,
            resource="batch_count",
            limit=context.batch_options.max_batches,
        )
    yield _build_batch(
        source=source,
        run_id=run_id,
        adapter_id=adapter_id,
        version=version,
        batch_index=batch_index,
        lines=(),
        blocks=(),
        record_count=0,
        prior_summaries=tuple(summaries),
        prior_indexed_refs=indexed_refs,
        options_fingerprint=options_fingerprint,
        is_last=True,
    )
