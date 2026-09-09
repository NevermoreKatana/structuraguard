"""Потоковый lossless adapter для CSV/TSV format family."""

from __future__ import annotations

import asyncio
import codecs
import unicodedata
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from fractions import Fraction
from io import StringIO
from typing import Final, cast

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.common import (
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    StringScalar,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedCell,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ExtractedTable,
    ExtractedValue,
    PhysicalMetadataEntry,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
    TabularCellLocation,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers._hashing import batch_fingerprint, manifest_fingerprint
from structuraguard.ports.source import ParseContext, ProbeContext, SourceReader

from ._common import (
    EncodingDetection,
    TextParserLimits,
    _context_encoding_detection,
    _read_checked,
    advisory_signals,
    binary_container_probe,
    decode_probe_sample,
    detect_encoding,
    encoding_error,
    extraction_id,
    is_text_like,
    limit_error,
    read_probe_sample,
)

_SCHEMA_VERSION: Final = "1.1.0"
_SDK_VERSION: Final = "0.3.0"
_PLACEHOLDER_FINGERPRINT: Final = "sha256:" + "0" * 64
_MAX_INDEXED_REFS: Final = 10_000
_MAX_TOKENIZER_SLICE_CHARS: Final = 4_096
_TABLE_ID: Final = "table-0"

_MAX_COLUMNS: Final = 10_000
_MAX_FIELD_SIZE: Final = 16 * 1024 * 1024
_MAX_RECORD_CHARS: Final = 32 * 1024 * 1024
_MAX_BATCH_CELLS: Final = 10_000
_MAX_BATCH_CHARS: Final = _MAX_RECORD_CHARS + 2
_MAX_HEADER_PROBE_ROWS: Final = 1_024
_MAX_DIALECT_CANDIDATES: Final = 128

_CSV_MEDIA_TYPES = frozenset({"text/csv", "application/csv"})
_TSV_MEDIA_TYPES = frozenset({"text/tab-separated-values", "text/tsv"})
_CSV_EXTENSIONS = frozenset({".csv"})
_TSV_EXTENSIONS = frozenset({".tsv", ".tab"})


def _positive_int(value: int, *, field_name: str, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(
            f"{field_name} должен быть положительным int не больше {maximum}"
        )


def _dialect_character(
    value: str | None,
    *,
    field_name: str,
    optional: bool,
    allow_tab: bool = False,
) -> None:
    if value is None and optional:
        return
    if type(value) is not str or len(value) != 1:
        raise ValueError(f"{field_name} должен быть одним Unicode character")
    character = value
    forbidden_category = unicodedata.category(character) in {
        "Cc",
        "Cf",
        "Cs",
        "Zl",
        "Zp",
    }
    if character in {"\x00", "\r", "\n"} or (
        forbidden_category and not (allow_tab and character == "\t")
    ):
        raise ValueError(f"{field_name} содержит недопустимый Unicode character")


@dataclass(frozen=True, slots=True, kw_only=True)
class DelimitedDialect:
    """Immutable physical dialect без locale и semantic schema."""

    delimiter: str
    quote_char: str | None = '"'
    escape_char: str | None = None
    double_quote: bool = True

    def __post_init__(self) -> None:
        _dialect_character(
            self.delimiter,
            field_name="delimiter",
            optional=False,
            allow_tab=True,
        )
        _dialect_character(self.quote_char, field_name="quote_char", optional=True)
        _dialect_character(self.escape_char, field_name="escape_char", optional=True)
        if type(self.double_quote) is not bool:
            raise ValueError("double_quote должен быть точным bool")
        syntax = tuple(
            character
            for character in (self.delimiter, self.quote_char, self.escape_char)
            if character is not None
        )
        if len(syntax) != len(set(syntax)):
            raise ValueError("delimiter, quote_char и escape_char должны различаться")


def _candidate_tuple(
    values: tuple[str | None, ...],
    *,
    field_name: str,
    optional: bool,
    allow_tab: bool = False,
) -> None:
    if type(values) is not tuple or not values:
        raise ValueError(f"{field_name} должен быть непустым tuple")
    if len(values) > _MAX_DIALECT_CANDIDATES:
        raise ValueError(
            f"{field_name} не должен содержать больше "
            f"{_MAX_DIALECT_CANDIDATES} candidates"
        )
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} не должен содержать дубликаты")
    for value in values:
        _dialect_character(
            value,
            field_name=field_name,
            optional=optional,
            allow_tab=allow_tab,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DelimitedDetectionOptions:
    """Конечный набор caller-controlled dialect candidates либо exact override."""

    delimiters: tuple[str, ...] = (",", "\t", ";", "|")
    quote_chars: tuple[str | None, ...] = ('"', "'")
    escape_chars: tuple[str | None, ...] = (None, "\\")
    dialect_override: DelimitedDialect | None = None

    def __post_init__(self) -> None:
        _candidate_tuple(
            cast(tuple[str | None, ...], self.delimiters),
            field_name="delimiters",
            optional=False,
            allow_tab=True,
        )
        _candidate_tuple(
            self.quote_chars,
            field_name="quote_chars",
            optional=True,
        )
        _candidate_tuple(
            self.escape_chars,
            field_name="escape_chars",
            optional=True,
        )
        if (
            self.dialect_override is not None
            and type(self.dialect_override) is not DelimitedDialect
        ):
            raise ValueError("dialect_override должен быть DelimitedDialect")


@dataclass(frozen=True, slots=True, kw_only=True)
class DelimitedParserLimits(TextParserLimits):
    """Instance-local пределы dialect probe и incremental row tokenizer."""

    max_columns: int = 500
    max_field_size: int = 1_000_000
    max_record_chars: int = 4_000_000
    max_batch_cells: int = 4_096
    max_batch_chars: int = 8 * 1024 * 1024
    max_header_probe_rows: int = 32
    max_dialect_candidates: int = 32

    def __post_init__(self) -> None:
        TextParserLimits.__post_init__(self)
        _positive_int(
            self.max_columns,
            field_name="max_columns",
            maximum=_MAX_COLUMNS,
        )
        _positive_int(
            self.max_field_size,
            field_name="max_field_size",
            maximum=_MAX_FIELD_SIZE,
        )
        _positive_int(
            self.max_record_chars,
            field_name="max_record_chars",
            maximum=_MAX_RECORD_CHARS,
        )
        _positive_int(
            self.max_batch_cells,
            field_name="max_batch_cells",
            maximum=_MAX_BATCH_CELLS,
        )
        _positive_int(
            self.max_batch_chars,
            field_name="max_batch_chars",
            maximum=_MAX_BATCH_CHARS,
        )
        if self.max_batch_cells < self.max_columns:
            raise ValueError("max_batch_cells не может быть меньше max_columns")
        if self.max_batch_chars < self.max_record_chars + 2:
            raise ValueError("max_batch_chars должен вмещать max_record_chars и CRLF")
        _positive_int(
            self.max_header_probe_rows,
            field_name="max_header_probe_rows",
            maximum=_MAX_HEADER_PROBE_ROWS,
        )
        _positive_int(
            self.max_dialect_candidates,
            field_name="max_dialect_candidates",
            maximum=_MAX_DIALECT_CANDIDATES,
        )


@dataclass(frozen=True, slots=True)
class _Field:
    value: str
    quoted: bool
    escaped: bool


@dataclass(frozen=True, slots=True)
class _Row:
    row_index: int
    fields: tuple[_Field, ...]
    field_count: int
    batch_chars: int
    line_start: int
    line_end: int
    is_source_last: bool = False


class _MalformedRow(Exception):
    def __init__(self, reason: str, *, record_number: int, line_number: int) -> None:
        self.reason = reason
        self.record_number = record_number
        self.line_number = line_number
        super().__init__(reason)


def _malformed_error(error: _MalformedRow) -> ParserError:
    return ParserError(
        error_code="PARSER_MALFORMED_INPUT",
        message="Delimited source содержит некорректную logical row.",
        details={
            "reason": error.reason,
            "record_number": error.record_number,
            "line_number": error.line_number,
        },
    )


class _RowTokenizer:
    """Instance-local strict CSV state machine без process-global csv state."""

    def __init__(
        self,
        dialect: DelimitedDialect,
        limits: DelimitedParserLimits,
        *,
        max_records: int,
        stop_after_records: int | None = None,
        retain_rows: bool = False,
        shape_only: bool = False,
    ) -> None:
        self._dialect = dialect
        self._limits = limits
        self._max_records = max_records
        self._stop_after_records = stop_after_records
        self._retain_rows = retain_rows
        self._shape_only = shape_only

        self._fields: list[_Field] = []
        self._field = StringIO()
        self._field_chars = 0
        self._field_count = 0
        self._field_quoted = False
        self._field_escaped = False
        self._field_started = False
        self._row_started = False
        self._in_quotes = False
        self._after_quote = False
        self._escaping = False
        self._pending_cr = False
        self._stopped = False

        self._record_chars = 0
        self._line_chars = 0
        self._line_number = 1
        self._record_start_line = 1
        self._last_record_content_line = 1
        self._record_count = 0
        self._completed_rows: list[_Row] = []

        self.delimiter_count = 0
        self._current_delimiter_count = 0
        self.quote_count = 0
        self.escape_count = 0

    @property
    def completed_rows(self) -> tuple[_Row, ...]:
        return tuple(self._completed_rows)

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def current_delimiter_count(self) -> int:
        return self._current_delimiter_count

    def _failure(self, reason: str) -> _MalformedRow:
        line_number = self._line_number
        if reason in {"dangling_escape", "unterminated_quote"}:
            line_number = self._last_record_content_line
        return _MalformedRow(
            reason,
            record_number=self._record_count + 1,
            line_number=line_number,
        )

    def _ensure_record_capacity(self) -> None:
        if self._record_count >= self._max_records:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="records",
                limit=self._max_records,
            )

    def _count_regular_character(self) -> None:
        self._ensure_record_capacity()
        if self._line_number > self._limits.max_lines:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="line_count",
                limit=self._limits.max_lines,
            )
        if self._line_chars >= self._limits.max_line_chars:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="line_chars",
                limit=self._limits.max_line_chars,
            )
        if self._record_chars >= self._limits.max_record_chars:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="record_chars",
                limit=self._limits.max_record_chars,
            )
        self._line_chars += 1
        self._record_chars += 1
        self._last_record_content_line = self._line_number

    def _append_value(self, value: str) -> None:
        if len(value) > self._limits.max_field_size - self._field_chars:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="field_size",
                limit=self._limits.max_field_size,
            )
        if not self._shape_only:
            self._field.write(value)
        self._field_chars += len(value)
        self._field_started = True
        self._row_started = True

    def _finish_field(self) -> None:
        if self._field_count >= self._limits.max_columns:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="columns",
                limit=self._limits.max_columns,
            )
        if not self._shape_only:
            self._fields.append(
                _Field(
                    self._field.getvalue(),
                    self._field_quoted,
                    self._field_escaped,
                )
            )
        self._field_count += 1
        self._field.seek(0)
        self._field.truncate(0)
        self._field_chars = 0
        self._field_quoted = False
        self._field_escaped = False
        self._field_started = False
        self._after_quote = False
        self._in_quotes = False

    def _reset_record(self) -> None:
        self._fields.clear()
        self._field.seek(0)
        self._field.truncate(0)
        self._field_chars = 0
        self._field_count = 0
        self._field_quoted = False
        self._field_escaped = False
        self._field_started = False
        self._row_started = False
        self._in_quotes = False
        self._after_quote = False
        self._escaping = False
        self._record_chars = 0
        self._current_delimiter_count = 0
        self._record_start_line = self._line_number
        self._last_record_content_line = self._line_number

    def _emit_row(self, *, line_end: int, terminator_chars: int = 0) -> _Row:
        self._ensure_record_capacity()
        if self._row_started or self._fields or self._field_started:
            self._finish_field()
            fields = tuple(self._fields)
            field_count = self._field_count
        else:
            fields = ()
            field_count = 0
        row = _Row(
            row_index=self._record_count,
            fields=fields,
            field_count=field_count,
            batch_chars=self._record_chars + terminator_chars,
            line_start=self._record_start_line,
            line_end=line_end,
        )
        self._record_count += 1
        if self._retain_rows:
            self._completed_rows.append(row)
        if (
            self._stop_after_records is not None
            and self._record_count >= self._stop_after_records
        ):
            self._stopped = True
        self._reset_record()
        return row

    def _consume_character(self, character: str) -> None:
        if character == "\x00":
            raise self._failure("binary_content")
        self._count_regular_character()
        self._row_started = True

        if self._escaping:
            if character not in {
                self._dialect.delimiter,
                self._dialect.quote_char,
                self._dialect.escape_char,
            }:
                raise self._failure("invalid_escape")
            if character != self._dialect.escape_char:
                self.escape_count += 1
            self._append_value(character)
            self._escaping = False
            return

        if self._in_quotes:
            if character == self._dialect.escape_char:
                self._escaping = True
                self._field_escaped = True
                return
            if character == self._dialect.quote_char:
                self._in_quotes = False
                self._after_quote = True
                return
            self._append_value(character)
            return

        if self._after_quote:
            if self._dialect.double_quote and character == self._dialect.quote_char:
                self._append_value(character)
                self._field_escaped = True
                self._in_quotes = True
                self._after_quote = False
                return
            if character == self._dialect.delimiter:
                self.delimiter_count += 1
                self._current_delimiter_count += 1
                self._finish_field()
                if self._field_count >= self._limits.max_columns:
                    raise limit_error(
                        adapter_id="builtin.delimited",
                        resource="columns",
                        limit=self._limits.max_columns,
                    )
                return
            raise self._failure("unexpected_character_after_quote")

        if character == self._dialect.escape_char:
            self._escaping = True
            self._field_escaped = True
            self._field_started = True
            return
        if character == self._dialect.delimiter:
            self.delimiter_count += 1
            self._current_delimiter_count += 1
            self._finish_field()
            if self._field_count >= self._limits.max_columns:
                raise limit_error(
                    adapter_id="builtin.delimited",
                    resource="columns",
                    limit=self._limits.max_columns,
                )
            return
        if character == self._dialect.quote_char:
            if self._field_started:
                raise self._failure("unexpected_quote")
            self._field_started = True
            self._field_quoted = True
            self._in_quotes = True
            self.quote_count += 1
            return
        self._append_value(character)

    def _consume_newline(self, newline: str) -> _Row | None:
        self._ensure_record_capacity()
        if self._line_number > self._limits.max_lines:
            raise limit_error(
                adapter_id="builtin.delimited",
                resource="line_count",
                limit=self._limits.max_lines,
            )

        if self._escaping:
            self.escape_count += 1
            if len(newline) > self._limits.max_record_chars - self._record_chars:
                raise limit_error(
                    adapter_id="builtin.delimited",
                    resource="record_chars",
                    limit=self._limits.max_record_chars,
                )
            self._record_chars += len(newline)
            self._last_record_content_line = self._line_number
            self._append_value(newline)
            self._escaping = False
            self._line_number += 1
            self._line_chars = 0
            return None

        if self._in_quotes:
            if len(newline) > self._limits.max_record_chars - self._record_chars:
                raise limit_error(
                    adapter_id="builtin.delimited",
                    resource="record_chars",
                    limit=self._limits.max_record_chars,
                )
            self._record_chars += len(newline)
            self._last_record_content_line = self._line_number
            self._append_value(newline)
            self._line_number += 1
            self._line_chars = 0
            return None

        row = self._emit_row(
            line_end=self._line_number,
            terminator_chars=len(newline),
        )
        self._line_number += 1
        self._line_chars = 0
        self._record_start_line = self._line_number
        return row

    def feed(self, text: str, *, final: bool) -> tuple[_Row, ...]:
        """Принять decoded chunk и вернуть только complete logical rows."""

        emitted: list[_Row] = []
        position = 0
        if self._pending_cr and (text or final):
            newline = "\r\n" if text.startswith("\n") else "\r"
            position = 1 if newline == "\r\n" else 0
            self._pending_cr = False
            row = self._consume_newline(newline)
            if row is not None:
                emitted.append(row)

        while position < len(text) and not self._stopped:
            character = text[position]
            if character == "\r":
                if position + 1 == len(text):
                    self._pending_cr = True
                    position += 1
                    break
                newline = "\r\n" if text[position + 1] == "\n" else "\r"
                position += len(newline)
                row = self._consume_newline(newline)
                if row is not None:
                    emitted.append(row)
                continue
            if character == "\n":
                position += 1
                row = self._consume_newline("\n")
                if row is not None:
                    emitted.append(row)
                continue
            self._consume_character(character)
            position += 1

        if final and not self._stopped:
            if self._pending_cr:
                self._pending_cr = False
                row = self._consume_newline("\r")
                if row is not None:
                    emitted.append(row)
            if self._escaping:
                raise self._failure("dangling_escape")
            if self._in_quotes:
                raise self._failure("unterminated_quote")
            if self._row_started or self._fields or self._field_started:
                emitted.append(self._emit_row(line_end=self._line_number))
            if emitted:
                terminal = replace(emitted[-1], is_source_last=True)
                emitted[-1] = terminal
                if self._retain_rows and self._completed_rows:
                    self._completed_rows[-1] = terminal
        return tuple(emitted)


@dataclass(frozen=True, slots=True)
class _Candidate:
    dialect: DelimitedDialect
    consistency: Fraction
    row_count: int
    quote_count: int
    escape_count: int
    order: int

    @property
    def score(self) -> tuple[Fraction, int, int]:
        return (
            self.consistency,
            int(self.quote_count > 0),
            self.row_count,
        )


@dataclass(frozen=True, slots=True)
class _CandidateFailure:
    dialect: DelimitedDialect
    error: _MalformedRow
    consistency: Fraction | None
    partial_consistency: Fraction | None
    current_delimiter_count: int
    delimiter_count: int
    quote_count: int
    escape_count: int
    order: int


@dataclass(frozen=True, slots=True)
class _CandidateLimit:
    dialect: DelimitedDialect
    error: SecurityPolicyError
    consistency: Fraction | None
    delimiter_count: int
    order: int


@dataclass(frozen=True, slots=True)
class _HeaderCandidate:
    row_index: int
    confidence: float
    duplicate_values: int


@dataclass(frozen=True, slots=True)
class _DialectDetection:
    dialect: DelimitedDialect
    header_candidate: _HeaderCandidate | None
    confidence: Decimal
    warnings: tuple[str, ...]


class _AmbiguousDialect(Exception):
    pass


def _dialects(
    options: DelimitedDetectionOptions,
    *,
    maximum: int,
) -> tuple[DelimitedDialect, ...]:
    if options.dialect_override is not None:
        return (options.dialect_override,)
    candidates: list[DelimitedDialect] = []
    for delimiter in options.delimiters:
        for quote_char in options.quote_chars:
            for escape_char in options.escape_chars:
                try:
                    candidate = DelimitedDialect(
                        delimiter=delimiter,
                        quote_char=quote_char,
                        escape_char=escape_char,
                    )
                except ValueError:
                    continue
                if len(candidates) >= maximum:
                    raise ValueError(
                        "Число dialect candidates превышает max_dialect_candidates"
                    )
                candidates.append(candidate)
    return tuple(candidates)


def _row_consistency(
    rows: tuple[_Row, ...],
    *,
    forced: bool,
) -> Fraction | None:
    nonblank = tuple(row for row in rows if row.field_count > 0)
    required_rows = 1 if forced else 2
    if len(nonblank) < required_rows:
        return None
    widths = Counter(row.field_count for row in nonblank)
    if not forced and sum(row.field_count >= 2 for row in nonblank) < 2:
        return None
    dominant_width, dominant_count = min(
        widths.items(),
        key=lambda item: (-item[1], -item[0]),
    )
    if dominant_width < 2 or (not forced and dominant_count < 2):
        return None
    return Fraction(dominant_count, len(nonblank))


def _header_candidate(rows: tuple[_Row, ...]) -> _HeaderCandidate | None:
    if not rows or not rows[0].fields:
        return None
    first = rows[0]
    values = tuple(field.value for field in first.fields)
    if len(values) < 2 or any(value == "" for value in values):
        return None
    duplicate_values = len(values) - len(set(values))
    corroborated = any(len(row.fields) == len(first.fields) for row in rows[1:])
    return _HeaderCandidate(
        row_index=first.row_index,
        confidence=(
            0.45
            if duplicate_values and corroborated
            else 0.30
            if duplicate_values
            else 0.60
            if corroborated
            else 0.35
        ),
        duplicate_values=duplicate_values,
    )


def _shape_detection_limits(
    text: str,
    limits: DelimitedParserLimits,
) -> DelimitedParserLimits:
    """Отделить bounded dialect evidence от caller operational caps."""

    sample_chars = max(len(text), 1)
    return replace(
        limits,
        max_columns=min(_MAX_COLUMNS, sample_chars + 1),
        max_field_size=min(_MAX_FIELD_SIZE, sample_chars),
        max_record_chars=min(_MAX_RECORD_CHARS, sample_chars),
        max_batch_cells=_MAX_BATCH_CELLS,
        max_batch_chars=_MAX_BATCH_CHARS,
        max_line_chars=min(16 * 1024 * 1024, sample_chars),
        max_lines=10_000_000,
    )


async def _feed_detection_text(
    tokenizer: _RowTokenizer,
    text: str,
    *,
    final: bool,
) -> None:
    """Ограничить latency одного candidate и дать task отмениться."""

    if not text:
        await asyncio.sleep(0)
        tokenizer.feed("", final=final)
        return
    for start in range(0, len(text), _MAX_TOKENIZER_SLICE_CHARS):
        await asyncio.sleep(0)
        end = min(start + _MAX_TOKENIZER_SLICE_CHARS, len(text))
        tokenizer.feed(text[start:end], final=final and end == len(text))
        if tokenizer.stopped:
            return


async def _detect_dialect(
    text: str,
    *,
    sample_is_complete: bool,
    options: DelimitedDetectionOptions,
    limits: DelimitedParserLimits,
) -> _DialectDetection | None:
    dialects = _dialects(options, maximum=limits.max_dialect_candidates)

    forced = options.dialect_override is not None
    if forced:
        dialect = options.dialect_override
        assert dialect is not None
        tokenizer = _RowTokenizer(
            dialect,
            limits,
            max_records=limits.max_header_probe_rows,
            stop_after_records=limits.max_header_probe_rows,
            retain_rows=True,
        )
        await _feed_detection_text(
            tokenizer,
            text,
            final=sample_is_complete,
        )
        forced_warnings: list[str] = []
        if tokenizer.quote_count == 0:
            forced_warnings.append("PARSER_DIALECT_QUOTE_INCONCLUSIVE")
        if tokenizer.escape_count == 0:
            forced_warnings.append("PARSER_DIALECT_ESCAPE_INCONCLUSIVE")
        return _DialectDetection(
            dialect=dialect,
            header_candidate=_header_candidate(tokenizer.completed_rows),
            confidence=Decimal("0.99"),
            warnings=tuple(forced_warnings),
        )

    shape_limits = _shape_detection_limits(text, limits)
    candidates: list[_Candidate] = []
    failures: list[_CandidateFailure] = []
    limit_failures: list[_CandidateLimit] = []
    for order, dialect in enumerate(dialects):
        tokenizer = _RowTokenizer(
            dialect,
            shape_limits,
            max_records=max(limits.max_header_probe_rows, 2),
            stop_after_records=limits.max_header_probe_rows,
            retain_rows=True,
            shape_only=True,
        )
        try:
            await _feed_detection_text(
                tokenizer,
                text,
                final=sample_is_complete,
            )
        except _MalformedRow as error:
            rows = tokenizer.completed_rows
            failures.append(
                _CandidateFailure(
                    dialect=dialect,
                    error=error,
                    consistency=_row_consistency(rows, forced=False),
                    partial_consistency=_row_consistency(rows, forced=True),
                    current_delimiter_count=tokenizer.current_delimiter_count,
                    delimiter_count=tokenizer.delimiter_count,
                    quote_count=tokenizer.quote_count,
                    escape_count=tokenizer.escape_count,
                    order=order,
                )
            )
            continue
        except SecurityPolicyError as error:
            rows = tokenizer.completed_rows
            limit_failures.append(
                _CandidateLimit(
                    dialect=dialect,
                    error=error,
                    consistency=_row_consistency(rows, forced=False),
                    delimiter_count=tokenizer.delimiter_count,
                    order=order,
                )
            )
            continue

        rows = tokenizer.completed_rows
        consistency = _row_consistency(rows, forced=False)
        if consistency is None or tokenizer.delimiter_count == 0:
            continue
        candidates.append(
            _Candidate(
                dialect=dialect,
                consistency=consistency,
                row_count=len(rows),
                quote_count=tokenizer.quote_count,
                escape_count=tokenizer.escape_count,
                order=order,
            )
        )

    if not candidates:
        plausible_failures = tuple(
            failure
            for failure in failures
            if failure.delimiter_count > 0
            and (failure.partial_consistency is not None or failure.quote_count > 0)
        )
        if plausible_failures:
            structured_failures = tuple(
                failure
                for failure in plausible_failures
                if failure.consistency is not None
            )
            if structured_failures:
                delimiters = {
                    failure.dialect.delimiter for failure in structured_failures
                }
                if len(delimiters) > 1:
                    raise _AmbiguousDialect
                malformed_candidate = min(
                    structured_failures,
                    key=lambda failure: (
                        -failure.quote_count,
                        -failure.escape_count,
                        failure.order,
                    ),
                )
                raise malformed_candidate.error
        if sample_is_complete and plausible_failures:
            delimiters = {failure.dialect.delimiter for failure in plausible_failures}
            if len(delimiters) == 1:
                malformed_candidate = min(
                    plausible_failures,
                    key=lambda failure: (
                        -failure.quote_count,
                        -failure.escape_count,
                        failure.order,
                    ),
                )
                raise malformed_candidate.error
        plausible_limits = tuple(
            failure for failure in limit_failures if failure.delimiter_count > 0
        )
        strong_limits = tuple(
            failure for failure in plausible_limits if failure.consistency is not None
        )
        if strong_limits:
            delimiters = {failure.dialect.delimiter for failure in strong_limits}
            if len(delimiters) == 1:
                strong_limit = min(
                    strong_limits,
                    key=lambda failure: failure.order,
                )
                validation_tokenizer = _RowTokenizer(
                    strong_limit.dialect,
                    limits,
                    max_records=max(limits.max_header_probe_rows, 2),
                    stop_after_records=limits.max_header_probe_rows,
                    shape_only=True,
                )
                try:
                    await _feed_detection_text(
                        validation_tokenizer,
                        text,
                        final=sample_is_complete,
                    )
                except SecurityPolicyError:
                    raise
                raise strong_limit.error
            raise _AmbiguousDialect
        return None

    best_score = max(candidate.score for candidate in candidates)
    strongest = tuple(
        candidate for candidate in candidates if candidate.score == best_score
    )
    if len({candidate.dialect.delimiter for candidate in strongest}) > 1:
        raise _AmbiguousDialect
    if (
        len(
            {
                candidate.dialect.quote_char
                for candidate in strongest
                if candidate.quote_count > 0
            }
        )
        > 1
    ):
        raise _AmbiguousDialect
    evidenced_escape_chars = {
        candidate.dialect.escape_char
        for candidate in strongest
        if candidate.escape_count > 0
    }
    if len(evidenced_escape_chars) > 1:
        raise _AmbiguousDialect
    if evidenced_escape_chars:
        evidenced_escape_char = next(iter(evidenced_escape_chars))
        if any(
            candidate.dialect.escape_char != evidenced_escape_char
            for candidate in strongest
        ):
            raise _AmbiguousDialect
        strongest = tuple(
            candidate
            for candidate in strongest
            if candidate.dialect.escape_char == evidenced_escape_char
        )
    else:
        no_escape_candidates = tuple(
            candidate
            for candidate in strongest
            if candidate.dialect.escape_char is None
        )
        if no_escape_candidates:
            strongest = no_escape_candidates
        elif len({candidate.dialect.escape_char for candidate in strongest}) > 1:
            raise _AmbiguousDialect
    selected_candidate = min(strongest, key=lambda candidate: candidate.order)
    quote_failures = tuple(
        failure
        for failure in failures
        if failure.error.reason
        in {"unexpected_character_after_quote", "unterminated_quote"}
        and failure.quote_count > 0
        and failure.delimiter_count > 0
    )
    cross_delimiter_quote_failures = tuple(
        failure
        for failure in quote_failures
        if failure.dialect.delimiter != selected_candidate.dialect.delimiter
        and (
            failure.consistency is not None
            or (
                failure.partial_consistency is not None
                and failure.current_delimiter_count > 0
            )
        )
    )
    if cross_delimiter_quote_failures:
        raise _AmbiguousDialect
    stronger_quote_failure = tuple(
        failure
        for failure in quote_failures
        if selected_candidate.quote_count == 0
        and failure.dialect.delimiter == selected_candidate.dialect.delimiter
        and failure.quote_count > selected_candidate.quote_count
    )
    if stronger_quote_failure:
        raise min(
            stronger_quote_failure,
            key=lambda failure: (-failure.quote_count, failure.order),
        ).error

    validation_tokenizer = _RowTokenizer(
        selected_candidate.dialect,
        limits,
        max_records=max(limits.max_header_probe_rows, 2),
        stop_after_records=limits.max_header_probe_rows,
        retain_rows=True,
    )
    await _feed_detection_text(
        validation_tokenizer,
        text,
        final=sample_is_complete,
    )

    confidence = Decimal(selected_candidate.consistency.numerator) / Decimal(
        selected_candidate.consistency.denominator
    )
    confidence *= Decimal("0.95" if selected_candidate.row_count >= 3 else "0.90")
    warnings: list[str] = []
    if selected_candidate.quote_count == 0:
        warnings.append("PARSER_DIALECT_QUOTE_INCONCLUSIVE")
    if selected_candidate.escape_count == 0:
        warnings.append("PARSER_DIALECT_ESCAPE_INCONCLUSIVE")
    return _DialectDetection(
        dialect=selected_candidate.dialect,
        header_candidate=_header_candidate(validation_tokenizer.completed_rows),
        confidence=confidence,
        warnings=tuple(warnings),
    )


def _merge_warnings(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for warning in group:
            if warning not in merged:
                merged.append(warning)
    return tuple(merged)


async def _read_parse_prefix(
    source: SourceArtifact,
    reader: SourceReader,
    limits: DelimitedParserLimits,
    *,
    minimal: bool,
) -> bytes:
    target = min(source.size_bytes, limits.encoding_probe_bytes)
    if minimal:
        target = min(target, 4)
    buffer = bytearray()
    offset = 0
    while offset < target:
        chunk = await _read_checked(
            reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, target - offset),
        )
        if not chunk:
            raise ParserError(
                error_code="PARSER_MALFORMED_INPUT",
                message="Source snapshot нарушает bounded parser input contract.",
                details={"reason": "unexpected_eof"},
            )
        buffer.extend(chunk)
        offset += len(chunk)
        await asyncio.sleep(0)
    return bytes(buffer)


def _parse_encoding(
    prefix: bytes,
    source: SourceArtifact,
    context: ParseContext,
    limits: DelimitedParserLimits,
) -> EncodingDetection:
    if context.detected_encoding is not None:
        return _context_encoding_detection(context.detected_encoding, sample=prefix)
    return detect_encoding(
        prefix,
        sample_is_complete=len(prefix) == source.size_bytes,
        limits=limits,
    )


async def _decoded_chunks(
    source: SourceArtifact,
    context: ParseContext,
    limits: DelimitedParserLimits,
    prefix: bytes,
    detection: EncodingDetection,
) -> AsyncIterator[tuple[str, bool]]:
    try:
        decoder = codecs.getincrementaldecoder(detection.codec)(errors="strict")
    except LookupError:
        raise encoding_error(
            "decode_failed",
            encoding=detection.reported_encoding,
        ) from None

    offset = 0
    chunk = prefix
    while True:
        offset += len(chunk)
        is_final = offset == source.size_bytes
        try:
            decoded = decoder.decode(chunk, final=is_final)
        except UnicodeDecodeError:
            raise encoding_error(
                "decode_failed",
                encoding=detection.reported_encoding,
            ) from None
        yield decoded, is_final
        if is_final:
            return
        await asyncio.sleep(0)
        chunk = await _read_checked(
            context.reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, source.size_bytes - offset),
        )
        if not chunk:
            raise ParserError(
                error_code="PARSER_MALFORMED_INPUT",
                message="Source snapshot нарушает bounded parser input contract.",
                details={"reason": "unexpected_eof"},
            )


async def _tokenized_rows(
    chunks: AsyncIterator[tuple[str, bool]],
    tokenizer: _RowTokenizer,
) -> AsyncIterator[_Row]:
    """Дробить caller-configurable read chunks для bounded latency/cancellation."""

    async for decoded, is_final in chunks:
        starts: range | tuple[int, ...] = (
            range(0, len(decoded), _MAX_TOKENIZER_SLICE_CHARS) if decoded else (0,)
        )
        for start in starts:
            end = min(start + _MAX_TOKENIZER_SLICE_CHARS, len(decoded))
            fragment_final = is_final and end == len(decoded)
            for row in tokenizer.feed(decoded[start:end], final=fragment_final):
                yield row
            await asyncio.sleep(0)


def _parser_options_fingerprint(
    limits: DelimitedParserLimits,
    detection_options: DelimitedDetectionOptions,
    context: ParseContext,
) -> str:
    payload = cast(
        CanonicalInput,
        {
            "batch_options": asdict(context.batch_options),
            "detection_options": asdict(detection_options),
            "detected_encoding": context.detected_encoding,
            "limits": asdict(limits),
        },
    )
    return canonical_sha256_value(payload)


def _table_metadata(
    detection: _DialectDetection,
    rows: Sequence[_Row],
) -> tuple[PhysicalMetadataEntry, ...]:
    widths = tuple(len(row.fields) for row in rows)
    header = detection.header_candidate
    entries = [
        PhysicalMetadataEntry(key="delimiter", value=detection.dialect.delimiter),
        PhysicalMetadataEntry(key="quote_char", value=detection.dialect.quote_char),
        PhysicalMetadataEntry(key="escape_char", value=detection.dialect.escape_char),
        PhysicalMetadataEntry(key="double_quote", value=detection.dialect.double_quote),
        PhysicalMetadataEntry(
            key="header_candidate",
            value=header is not None,
        ),
        PhysicalMetadataEntry(
            key="header_candidate_row",
            value=header.row_index if header is not None else None,
        ),
        PhysicalMetadataEntry(
            key="header_candidate_confidence",
            value=header.confidence if header is not None else None,
        ),
        PhysicalMetadataEntry(
            key="header_candidate_duplicate_values",
            value=header.duplicate_values if header is not None else None,
        ),
        PhysicalMetadataEntry(key="row_count", value=len(rows)),
        PhysicalMetadataEntry(key="minimum_column_count", value=min(widths)),
        PhysicalMetadataEntry(key="maximum_column_count", value=max(widths)),
        PhysicalMetadataEntry(key="ragged_rows", value=len(set(widths)) > 1),
        PhysicalMetadataEntry(
            key="blank_row_count",
            value=sum(not row.fields for row in rows),
        ),
    ]
    return tuple(entries)


def _table_for_rows(
    source: SourceArtifact,
    rows: Sequence[_Row],
    detection: _DialectDetection,
    *,
    segment_index: int,
    is_last: bool,
    first_cell_number: int,
) -> tuple[ExtractedTable, int]:
    first = rows[0]
    last = rows[-1]
    cells: list[ExtractedCell] = []
    next_cell_number = first_cell_number
    for row in rows:
        for column_index, field in enumerate(row.fields):
            location = TabularCellLocation(
                source=source.ref,
                table_id=_TABLE_ID,
                row_index=row.row_index,
                column_index=column_index,
            )
            cells.append(
                ExtractedCell(
                    cell_id=f"cell-{next_cell_number}",
                    row_index=row.row_index,
                    column_index=column_index,
                    value=ExtractedValue(
                        value_id=f"value-{next_cell_number}",
                        raw_value=StringScalar(value=field.value),
                        location=location,
                    ),
                    metadata=(
                        PhysicalMetadataEntry(key="quoted", value=field.quoted),
                        PhysicalMetadataEntry(key="escaped", value=field.escaped),
                        PhysicalMetadataEntry(
                            key="explicit_empty",
                            value=field.value == "",
                        ),
                    ),
                )
            )
            next_cell_number += 1
    table = ExtractedTable(
        table_id=_TABLE_ID,
        location=TabularCellLocation(
            source=source.ref,
            table_id=_TABLE_ID,
            row_index=first.row_index,
            column_index=0,
        ),
        cells=tuple(cells),
        segment_index=segment_index,
        row_start_index=first.row_index,
        row_end_index=last.row_index,
        is_last_segment=is_last,
        metadata=_table_metadata(detection, rows),
    )
    return table, next_cell_number


def _bounded_indexed_refs(
    *,
    run_id: str,
    batch_index: int,
    table: ExtractedTable | None,
    limit: int,
) -> tuple[PhysicalSourceRef, ...]:
    if table is None or limit <= 0:
        return ()
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

    include(PhysicalObjectKind.TABLE, table.table_id)
    for cell in table.cells:
        if not include(PhysicalObjectKind.CELL, cell.cell_id):
            return tuple(refs)
    for cell in table.cells:
        if not include(PhysicalObjectKind.VALUE, cell.value.value_id):
            return tuple(refs)
    return tuple(refs)


def _build_batch(
    *,
    source: SourceArtifact,
    run_id: str,
    batch_index: int,
    table: ExtractedTable | None,
    record_count: int,
    prior_summaries: tuple[ExtractedBatchSummary, ...],
    prior_indexed_refs: Sequence[PhysicalSourceRef],
    options_fingerprint: str,
    is_last: bool,
) -> ExtractedBatch:
    indexed_refs = _bounded_indexed_refs(
        run_id=run_id,
        batch_index=batch_index,
        table=table,
        limit=_MAX_INDEXED_REFS - len(prior_indexed_refs),
    )
    draft = ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=run_id,
        batch_index=batch_index,
        source=source.ref,
        parser_id="builtin.delimited",
        parser_version="1.0.0",
        batch_fingerprint=_PLACEHOLDER_FINGERPRINT,
        tables=(table,) if table is not None else (),
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
            parser_id="builtin.delimited",
            parser_version="1.0.0",
            producer=ProducerMetadata(
                component_id="builtin.delimited",
                component_version="1.0.0",
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
            update={"extraction_fingerprint": manifest_fingerprint(manifest_seed)}
        )
    return ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=run_id,
        batch_index=batch_index,
        source=source.ref,
        parser_id="builtin.delimited",
        parser_version="1.0.0",
        batch_fingerprint=fingerprint,
        tables=(table,) if table is not None else (),
        record_count=record_count,
        indexed_refs=indexed_refs,
        is_last=is_last,
        manifest=manifest,
    )


class DelimitedTextParser:
    """Извлечь CSV/TSV rows и raw cells без выбора бизнес-схемы.

    Args:
        limits: Неизменяемые DelimitedParserLimits; ``None`` выбирает defaults.
        detection_options: Конечные dialect candidates или точный override;
            ``None`` выбирает DelimitedDetectionOptions().

    Probe возвращает bounded dialect evidence. Parse использует reader/лимиты
    context и выдаёт ExtractedBatch с table segments между полными rows.
    Header — только кандидат; duplicate/empty values не переименовываются,
    отсутствующие ragged cells не достраиваются. Формулы не выполняются.

    Raises:
        ValueError: Параметры конструктора неверного типа.
        ParserError: Неоднозначен dialect (``PARSER_UNSUPPORTED_FEATURE``),
            некорректна строка (``PARSER_MALFORMED_INPUT`` с one-based line_number)
            или кодировка (``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: Превышены budgets (``SECURITY_LIMIT_EXCEEDED``).

    Чтение parse и его ошибки отложены до итерации; cancellation не подавляется.
    """

    adapter_id = "builtin.delimited"
    version = "1.0.0"
    priority = 30

    def __init__(
        self,
        *,
        limits: DelimitedParserLimits | None = None,
        detection_options: DelimitedDetectionOptions | None = None,
    ) -> None:
        self._limits = DelimitedParserLimits() if limits is None else limits
        self._detection_options = (
            DelimitedDetectionOptions()
            if detection_options is None
            else detection_options
        )
        if type(self._limits) is not DelimitedParserLimits:
            raise ValueError("limits должен быть DelimitedParserLimits")
        if type(self._detection_options) is not DelimitedDetectionOptions:
            raise ValueError("detection_options должен быть DelimitedDetectionOptions")
        dialects = _dialects(
            self._detection_options,
            maximum=self._limits.max_dialect_candidates,
        )
        if not dialects:
            raise ValueError("Detection options не образуют допустимый dialect")
        if (
            self._detection_options.dialect_override is None
            and self._limits.max_header_probe_rows < 2
        ):
            raise ValueError(
                "Auto-detection требует max_header_probe_rows не меньше двух"
            )

    @property
    def limits(self) -> DelimitedParserLimits:
        """Вернуть immutable instance-local resource limits."""

        return self._limits

    @property
    def detection_options(self) -> DelimitedDetectionOptions:
        """Вернуть immutable caller-controlled detection options."""

        return self._detection_options

    async def probe(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        """Подтвердить bounded repeated delimited structure по content."""

        sample = await read_probe_sample(source, context, self._limits)
        binary = binary_container_probe(
            sample,
            source=source,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
        )
        if binary is not None:
            return binary
        text, encoding = decode_probe_sample(
            sample,
            source_size=source.size_bytes,
            limits=self._limits,
        )
        detection: _DialectDetection | None = None
        if is_text_like(text) and source.size_bytes > 0:
            try:
                detection = await _detect_dialect(
                    text,
                    sample_is_complete=len(sample) == source.size_bytes,
                    options=self._detection_options,
                    limits=self._limits,
                )
            except _MalformedRow as error:
                raise _malformed_error(error) from None
            except _AmbiguousDialect:
                raise ParserError(
                    error_code="PARSER_UNSUPPORTED_FEATURE",
                    message="Delimited dialect неоднозначен.",
                    details={
                        "feature": "ambiguous_dialect",
                        "reason": "insufficient_structure",
                    },
                ) from None

        supported = detection is not None
        if detection is not None:
            is_tsv = detection.dialect.delimiter == "\t"
            media_types = _TSV_MEDIA_TYPES if is_tsv else _CSV_MEDIA_TYPES
            extensions = _TSV_EXTENSIONS if is_tsv else _CSV_EXTENSIONS
            format_id = "tsv" if is_tsv else "csv"
            detected_media_type = "text/tab-separated-values" if is_tsv else "text/csv"
            confidence = min(encoding.confidence, detection.confidence)
            warnings = _merge_warnings(encoding.warnings, detection.warnings)
        else:
            media_types = _CSV_MEDIA_TYPES | _TSV_MEDIA_TYPES
            extensions = _CSV_EXTENSIONS | _TSV_EXTENSIONS
            format_id = None
            detected_media_type = None
            confidence = Decimal("0")
            warnings = _merge_warnings(
                encoding.warnings,
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
            media_types=media_types,
            extensions=extensions,
        )
        return ProbeResult(
            source=source.ref,
            adapter_id=self.adapter_id,
            adapter_version=self.version,
            supported=supported,
            confidence=confidence,
            detected_media_type=detected_media_type,
            detected_encoding=encoding.reported_encoding,
            warnings=warnings,
            format_id=format_id,
            signals=(structure, *advisory),
        )

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        """Потоково извлечь raw rows/cells без header/schema normalization."""

        return self._parse(source, context)

    async def _parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        if source.size_bytes > context.max_bytes:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="source_bytes",
                limit=context.max_bytes,
            )

        options_fingerprint = _parser_options_fingerprint(
            self._limits,
            self._detection_options,
            context,
        )
        run_id = extraction_id(
            source,
            adapter_id=self.adapter_id,
            version=self.version,
            options_fingerprint=options_fingerprint,
        )
        if source.size_bytes == 0:
            yield _build_batch(
                source=source,
                run_id=run_id,
                batch_index=0,
                table=None,
                record_count=0,
                prior_summaries=(),
                prior_indexed_refs=(),
                options_fingerprint=options_fingerprint,
                is_last=True,
            )
            return

        prefix = await _read_parse_prefix(
            source,
            context.reader,
            self._limits,
            minimal=(
                context.detected_encoding is not None
                and self._detection_options.dialect_override is not None
            ),
        )
        encoding = _parse_encoding(prefix, source, context, self._limits)
        try:
            prefix_decoder = codecs.getincrementaldecoder(encoding.codec)(
                errors="strict"
            )
            prefix_text = prefix_decoder.decode(
                prefix,
                final=len(prefix) == source.size_bytes,
            )
        except (LookupError, UnicodeDecodeError):
            raise encoding_error(
                "decode_failed",
                encoding=encoding.reported_encoding,
            ) from None
        try:
            detection = await _detect_dialect(
                prefix_text,
                sample_is_complete=len(prefix) == source.size_bytes,
                options=self._detection_options,
                limits=self._limits,
            )
        except _MalformedRow as error:
            raise _malformed_error(error) from None
        except _AmbiguousDialect:
            raise ParserError(
                error_code="PARSER_UNSUPPORTED_FEATURE",
                message="Delimited dialect неоднозначен.",
                details={
                    "feature": "ambiguous_dialect",
                    "reason": "insufficient_structure",
                },
            ) from None
        if detection is None:
            raise ParserError(
                error_code="PARSER_UNSUPPORTED_FEATURE",
                message="Delimited dialect не подтверждён по content.",
                details={
                    "feature": "dialect_detection",
                    "reason": "insufficient_structure",
                },
            )

        tokenizer = _RowTokenizer(
            detection.dialect,
            self._limits,
            max_records=context.max_records,
        )
        summaries: list[ExtractedBatchSummary] = []
        indexed_refs: list[PhysicalSourceRef] = []
        pending_rows: list[_Row] = []
        pending_cell_count = 0
        pending_record_chars = 0
        total_physical = 0
        batch_index = 0
        next_cell_number = 0

        try:
            async for row in _tokenized_rows(
                _decoded_chunks(
                    source,
                    context,
                    self._limits,
                    prefix,
                    encoding,
                ),
                tokenizer,
            ):
                if detection.header_candidate is None and row.row_index == 0:
                    detection = replace(
                        detection,
                        header_candidate=_header_candidate((row,)),
                    )
                row_cell_count = len(row.fields)
                if pending_rows and (
                    pending_cell_count + row_cell_count > self._limits.max_batch_cells
                    or pending_record_chars + row.batch_chars
                    > self._limits.max_batch_chars
                ):
                    if batch_index + 1 >= context.batch_options.max_batches:
                        raise limit_error(
                            adapter_id=self.adapter_id,
                            resource="batch_count",
                            limit=context.batch_options.max_batches,
                        )
                    table, next_cell_number = _table_for_rows(
                        source,
                        pending_rows,
                        detection,
                        segment_index=batch_index,
                        is_last=False,
                        first_cell_number=next_cell_number,
                    )
                    batch = _build_batch(
                        source=source,
                        run_id=run_id,
                        batch_index=batch_index,
                        table=table,
                        record_count=len(pending_rows),
                        prior_summaries=tuple(summaries),
                        prior_indexed_refs=indexed_refs,
                        options_fingerprint=options_fingerprint,
                        is_last=False,
                    )
                    summaries.append(batch.to_summary())
                    indexed_refs.extend(batch.indexed_refs)
                    yield batch
                    pending_rows.clear()
                    pending_cell_count = 0
                    pending_record_chars = 0
                    batch_index += 1
                    await asyncio.sleep(0)

                next_physical = 2 * row_cell_count
                if not pending_rows:
                    next_physical += 1
                if next_physical > context.max_physical_objects - total_physical:
                    raise limit_error(
                        adapter_id=self.adapter_id,
                        resource="physical_objects",
                        limit=context.max_physical_objects,
                    )
                pending_rows.append(row)
                pending_cell_count += row_cell_count
                pending_record_chars += row.batch_chars
                total_physical += next_physical

                batch_full = (
                    len(pending_rows) >= context.batch_options.batch_size
                    or pending_cell_count >= self._limits.max_batch_cells
                    or pending_record_chars >= self._limits.max_batch_chars
                )
                if not batch_full and not row.is_source_last:
                    continue
                if batch_index >= context.batch_options.max_batches or (
                    not row.is_source_last
                    and batch_index + 1 >= context.batch_options.max_batches
                ):
                    raise limit_error(
                        adapter_id=self.adapter_id,
                        resource="batch_count",
                        limit=context.batch_options.max_batches,
                    )
                table, next_cell_number = _table_for_rows(
                    source,
                    pending_rows,
                    detection,
                    segment_index=batch_index,
                    is_last=row.is_source_last,
                    first_cell_number=next_cell_number,
                )
                batch = _build_batch(
                    source=source,
                    run_id=run_id,
                    batch_index=batch_index,
                    table=table,
                    record_count=len(pending_rows),
                    prior_summaries=tuple(summaries),
                    prior_indexed_refs=indexed_refs,
                    options_fingerprint=options_fingerprint,
                    is_last=row.is_source_last,
                )
                if not row.is_source_last:
                    summaries.append(batch.to_summary())
                    indexed_refs.extend(batch.indexed_refs)
                yield batch
                if row.is_source_last:
                    return
                pending_rows.clear()
                pending_cell_count = 0
                pending_record_chars = 0
                batch_index += 1
                await asyncio.sleep(0)
        except _MalformedRow as error:
            raise _malformed_error(error) from None

        if pending_rows:
            if batch_index >= context.batch_options.max_batches:
                raise limit_error(
                    adapter_id=self.adapter_id,
                    resource="batch_count",
                    limit=context.batch_options.max_batches,
                )
            table, next_cell_number = _table_for_rows(
                source,
                pending_rows,
                detection,
                segment_index=batch_index,
                is_last=True,
                first_cell_number=next_cell_number,
            )
            del next_cell_number
            yield _build_batch(
                source=source,
                run_id=run_id,
                batch_index=batch_index,
                table=table,
                record_count=len(pending_rows),
                prior_summaries=tuple(summaries),
                prior_indexed_refs=indexed_refs,
                options_fingerprint=options_fingerprint,
                is_last=True,
            )
            return

        if not summaries:
            yield _build_batch(
                source=source,
                run_id=run_id,
                batch_index=0,
                table=None,
                record_count=0,
                prior_summaries=(),
                prior_indexed_refs=(),
                options_fingerprint=options_fingerprint,
                is_last=True,
            )


__all__ = (
    "DelimitedDetectionOptions",
    "DelimitedDialect",
    "DelimitedParserLimits",
    "DelimitedTextParser",
)
