"""Bounded JSON primitives shared only by JSON document/lines adapters."""

from __future__ import annotations

import asyncio
import codecs
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, Protocol, cast

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.common import (
    BooleanScalar,
    NullScalar,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    StringScalar,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ExtractedTreeNode,
    ExtractedValue,
    JsonPointerLocation,
    PhysicalNodeKind,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError
from structuraguard.parsers._hashing import batch_fingerprint, manifest_fingerprint
from structuraguard.ports.source import ParseContext, ProbeContext, SourceReader

from ._common import (
    advisory_signals,
    encoding_error,
    extraction_id,
    has_binary_container_signature,
    limit_error,
)

_SCHEMA_VERSION: Final = "1.1.0"
_SDK_VERSION: Final = "0.3.0"
_PLACEHOLDER_FINGERPRINT: Final = "sha256:" + "0" * 64
_MAX_INDEXED_REFS: Final = 10_000
_CHECKPOINT_CHARS: Final = 4_096

_MAX_READ_CHUNK_BYTES: Final = 1024 * 1024
_MAX_PROBE_BYTES: Final = 1024 * 1024
_MAX_PROBE_RECORDS: Final = 1_024
_MAX_RECORD_CHARS: Final = 32 * 1024 * 1024
# Совпадает с hard limit raw_name в ExtractedTreeNode: ключ объекта не должен
# пройти lexer, а затем неожиданно упасть на валидации публичного контракта.
_MAX_VALUE_CHARS: Final = 1024 * 1024
_MAX_NUMBER_CHARS: Final = 1024 * 1024
_MAX_KEYS: Final = 10_000_000
_MAX_NODES: Final = 10_000_000
_MAX_ARRAY_ITEMS: Final = 10_000_000
_MAX_BATCH_NODES: Final = 100_000
_MAX_BATCH_CHARS: Final = 32 * 1024 * 1024
_MAX_NESTING_DEPTH: Final = 256
_MAX_POINTER_CHARS: Final = 4_096

_JSON_WHITESPACE: Final = frozenset({" ", "\t", "\r", "\n"})
_JSON_TOKEN_STARTS: Final = frozenset('{["-0123456789tfn')
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")

JSON_MEDIA_TYPES: Final = frozenset(
    {"application/json", "text/json", "application/problem+json"}
)
JSON_EXTENSIONS: Final = frozenset({".json"})
JSON_LINES_MEDIA_TYPES: Final = frozenset(
    {
        "application/jsonl",
        "application/ndjson",
        "application/x-jsonlines",
        "application/x-ndjson",
    }
)
JSON_LINES_EXTENSIONS: Final = frozenset({".jsonl", ".ndjson"})


def _positive_int(value: int, *, field_name: str, maximum: int) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError(
            f"{field_name} должен быть положительным int не больше {maximum}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class JsonParserLimits:
    """Неизменяемые пределы лексического и древовидного извлечения JSON.

    Поля ``*_bytes`` измеряются в байтах, ``*_chars`` — в Unicode code points;
    nodes, keys и array items считаются отдельно. ``max_nesting_depth`` действует
    совместно с пределом ``ParseContext``. Batch содержит только целые поддеревья;
    превышение бюджета неделимой записи не разрешает её обрезать.

    Raises:
        ValueError: Предел не является положительным int или превышает hard cap.
    """

    read_chunk_bytes: int = 64 * 1024
    max_probe_bytes: int = 256 * 1024
    max_probe_records: int = 32
    max_record_chars: int = 4_000_000
    max_value_chars: int = 1_000_000
    max_number_chars: int = 4_096
    max_keys: int = 1_000_000
    max_nodes: int = 1_000_000
    max_array_items: int = 1_000_000
    max_batch_nodes: int = 10_000
    max_batch_chars: int = 8 * 1024 * 1024
    max_nesting_depth: int = 64
    max_pointer_chars: int = _MAX_POINTER_CHARS

    def __post_init__(self) -> None:
        limits = (
            ("read_chunk_bytes", self.read_chunk_bytes, _MAX_READ_CHUNK_BYTES),
            ("max_probe_bytes", self.max_probe_bytes, _MAX_PROBE_BYTES),
            ("max_probe_records", self.max_probe_records, _MAX_PROBE_RECORDS),
            ("max_record_chars", self.max_record_chars, _MAX_RECORD_CHARS),
            ("max_value_chars", self.max_value_chars, _MAX_VALUE_CHARS),
            ("max_number_chars", self.max_number_chars, _MAX_NUMBER_CHARS),
            ("max_keys", self.max_keys, _MAX_KEYS),
            ("max_nodes", self.max_nodes, _MAX_NODES),
            ("max_array_items", self.max_array_items, _MAX_ARRAY_ITEMS),
            ("max_batch_nodes", self.max_batch_nodes, _MAX_BATCH_NODES),
            ("max_batch_chars", self.max_batch_chars, _MAX_BATCH_CHARS),
            ("max_nesting_depth", self.max_nesting_depth, _MAX_NESTING_DEPTH),
            ("max_pointer_chars", self.max_pointer_chars, _MAX_POINTER_CHARS),
        )
        for field_name, value, maximum in limits:
            _positive_int(value, field_name=field_name, maximum=maximum)


@dataclass(frozen=True, slots=True)
class _Position:
    index: int
    line: int
    column: int


@dataclass(frozen=True, slots=True)
class _Character:
    value: str
    start: _Position
    end: _Position


class _Characters(Protocol):
    @property
    def position(self) -> _Position: ...

    async def peek(self) -> _Character | None: ...

    async def take(self) -> _Character | None: ...


class _StringCharacters:
    """Async-compatible bounded character source for one decoded record."""

    def __init__(self, value: str, *, line_number: int = 1) -> None:
        self._value = value
        self._offset = 0
        self._line = line_number
        self._column = 0
        self._previous_cr = False
        self._peeked: _Character | None = None
        self._checkpoint = 0

    @property
    def position(self) -> _Position:
        return _Position(self._offset, self._line, self._column)

    async def peek(self) -> _Character | None:
        if self._peeked is None:
            self._peeked = await self._next()
        return self._peeked

    async def take(self) -> _Character | None:
        candidate = await self.peek()
        self._peeked = None
        return candidate

    async def _next(self) -> _Character | None:
        if self._offset >= len(self._value):
            return None
        start = self.position
        character = self._value[self._offset]
        self._offset += 1
        self._advance(character)
        self._checkpoint += 1
        if self._checkpoint >= _CHECKPOINT_CHARS:
            self._checkpoint = 0
            await asyncio.sleep(0)
        return _Character(character, start, self.position)

    def _advance(self, character: str) -> None:
        if character == "\r":
            self._line += 1
            self._column = 0
            self._previous_cr = True
            return
        if character == "\n":
            if not self._previous_cr:
                self._line += 1
            self._column = 0
            self._previous_cr = False
            return
        self._column += 1
        self._previous_cr = False


async def _checked_read(
    reader: SourceReader,
    *,
    offset: int,
    size: int,
) -> bytes:
    returned = await reader.read(offset=offset, size=size)
    if type(returned) is not bytes:
        raise _malformed("reader_result_type", line_number=1)
    if len(returned) > size:
        raise _malformed("reader_result_size", line_number=1)
    return returned


class _SourceCharacters:
    """Strict incremental UTF-8 decoder with exact line/column positions."""

    def __init__(
        self,
        source: SourceArtifact,
        context: ParseContext,
        limits: JsonParserLimits,
        *,
        adapter_id: str,
    ) -> None:
        if source.size_bytes > context.max_bytes:
            raise limit_error(
                adapter_id=adapter_id,
                resource="source_bytes",
                limit=context.max_bytes,
            )
        if (
            context.detected_encoding is not None
            and _canonical_json_encoding(context.detected_encoding) is None
        ):
            raise encoding_error(
                "unsupported_codec", encoding=context.detected_encoding
            )
        self._source = source
        self._context = context
        self._limits = limits
        self._reader = context.reader
        self._decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
        self._byte_offset = 0
        self._decoded = ""
        self._decoded_offset = 0
        self._finalized = False
        self._char_index = 0
        self._line = 1
        self._column = 0
        self._previous_cr = False
        self._peeked: _Character | None = None
        self._checkpoint = 0

    @property
    def position(self) -> _Position:
        return _Position(self._char_index, self._line, self._column)

    async def peek(self) -> _Character | None:
        if self._peeked is None:
            self._peeked = await self._next()
        return self._peeked

    async def take(self) -> _Character | None:
        candidate = await self.peek()
        self._peeked = None
        return candidate

    async def _next(self) -> _Character | None:
        while self._decoded_offset >= len(self._decoded):
            if self._finalized:
                return None
            await self._fill()
        start = self.position
        character = self._decoded[self._decoded_offset]
        self._decoded_offset += 1
        self._char_index += 1
        self._advance(character)
        self._checkpoint += 1
        if self._checkpoint >= _CHECKPOINT_CHARS:
            self._checkpoint = 0
            await asyncio.sleep(0)
        return _Character(character, start, self.position)

    async def _fill(self) -> None:
        if self._byte_offset == self._source.size_bytes:
            try:
                self._decoded = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                raise encoding_error("decode_failed", encoding="utf-8") from None
            self._decoded_offset = 0
            self._finalized = True
            return
        requested = min(
            self._limits.read_chunk_bytes,
            self._source.size_bytes - self._byte_offset,
        )
        chunk = await _checked_read(
            self._reader,
            offset=self._byte_offset,
            size=requested,
        )
        if not chunk:
            raise _malformed("unexpected_eof", line_number=self._line)
        self._byte_offset += len(chunk)
        try:
            self._decoded = self._decoder.decode(
                chunk,
                final=self._byte_offset == self._source.size_bytes,
            )
        except UnicodeDecodeError:
            raise encoding_error("decode_failed", encoding="utf-8") from None
        self._decoded_offset = 0
        if self._byte_offset == self._source.size_bytes:
            self._finalized = True
        if not self._decoded and not self._finalized:
            await asyncio.sleep(0)

    def _advance(self, character: str) -> None:
        if character == "\r":
            self._line += 1
            self._column = 0
            self._previous_cr = True
            return
        if character == "\n":
            if not self._previous_cr:
                self._line += 1
            self._column = 0
            self._previous_cr = False
            return
        self._column += 1
        self._previous_cr = False


def _canonical_json_encoding(value: str) -> str | None:
    try:
        canonical = codecs.lookup(value).name.replace("_", "-")
    except LookupError:
        return None
    if canonical in {"utf-8", "utf-8-sig"}:
        return canonical
    return None


class _TokenKind(StrEnum):
    OBJECT_START = "object_start"
    OBJECT_END = "object_end"
    ARRAY_START = "array_start"
    ARRAY_END = "array_end"
    COLON = "colon"
    COMMA = "comma"
    STRING = "string"
    NUMBER = "number"
    TRUE = "true"
    FALSE = "false"
    NULL = "null"
    EOF = "eof"


@dataclass(frozen=True, slots=True)
class _Token:
    kind: _TokenKind
    value: str | bool | None
    start: _Position
    end: _Position


class _JsonSyntax(Exception):
    def __init__(self, reason: str, *, line_number: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.line_number = line_number


class _JsonLexer:
    """Strict JSON lexer without regex, float conversion or global state."""

    def __init__(
        self,
        characters: _Characters,
        limits: JsonParserLimits,
        *,
        adapter_id: str,
    ) -> None:
        self._characters = characters
        self._limits = limits
        self._adapter_id = adapter_id
        self._record_start: int | None = None

    @property
    def position(self) -> _Position:
        return self._characters.position

    def begin_record(self) -> None:
        self._record_start = self.position.index

    def end_record(self, end: _Position) -> int:
        start = self._record_start
        self._record_start = None
        return 0 if start is None else end.index - start

    def abandon_record(self) -> None:
        self._record_start = None

    async def next_after_record(self) -> tuple[_Token, int]:
        """Завершить record после whitespace, но до следующего JSON token."""

        character = await self._peek()
        while character is not None and character.value in _JSON_WHITESPACE:
            await self._take()
            character = await self._peek()
        boundary = self.position if character is None else character.start
        source_chars = self.end_record(boundary)
        return await self.next(), source_chars

    async def next(self) -> _Token:
        character = await self._peek()
        while character is not None and character.value in _JSON_WHITESPACE:
            await self._take()
            character = await self._peek()
        if character is None:
            position = self.position
            return _Token(_TokenKind.EOF, None, position, position)

        punctuation = {
            "{": _TokenKind.OBJECT_START,
            "}": _TokenKind.OBJECT_END,
            "[": _TokenKind.ARRAY_START,
            "]": _TokenKind.ARRAY_END,
            ":": _TokenKind.COLON,
            ",": _TokenKind.COMMA,
        }
        if character.value in punctuation:
            consumed = await self._take()
            assert consumed is not None
            return _Token(
                punctuation[character.value],
                character.value,
                consumed.start,
                consumed.end,
            )
        if character.value == '"':
            return await self._string()
        if character.value == "-" or (
            character.value.isascii() and character.value.isdigit()
        ):
            return await self._number()
        if character.value == "t":
            return await self._literal("true", _TokenKind.TRUE, True)
        if character.value == "f":
            return await self._literal("false", _TokenKind.FALSE, False)
        if character.value == "n":
            return await self._literal("null", _TokenKind.NULL, None)
        raise _JsonSyntax("invalid_json", line_number=character.start.line)

    async def _peek(self) -> _Character | None:
        return await self._characters.peek()

    async def _take(self) -> _Character | None:
        character = await self._characters.take()
        if character is None or self._record_start is None:
            return character
        if character.end.index - self._record_start > self._limits.max_record_chars:
            raise limit_error(
                adapter_id=self._adapter_id,
                resource="record_chars",
                limit=self._limits.max_record_chars,
            )
        return character

    async def _string(self) -> _Token:
        opening = await self._take()
        assert opening is not None
        decoded: list[str] = []
        source_chars = 1
        while True:
            character = await self._take()
            if character is None:
                raise _JsonSyntax("unexpected_eof", line_number=opening.start.line)
            source_chars += 1
            self._check_value_size(source_chars)
            if character.value == '"':
                return _Token(
                    _TokenKind.STRING,
                    "".join(decoded),
                    opening.start,
                    character.end,
                )
            if ord(character.value) < 0x20:
                raise _JsonSyntax(
                    "invalid_json",
                    line_number=character.start.line,
                )
            if character.value != "\\":
                decoded.append(character.value)
                self._check_value_size(len(decoded))
                continue
            escaped = await self._take()
            if escaped is None:
                raise _JsonSyntax("unexpected_eof", line_number=character.start.line)
            source_chars += 1
            self._check_value_size(source_chars)
            simple = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }
            if escaped.value in simple:
                decoded.append(simple[escaped.value])
                self._check_value_size(len(decoded))
                continue
            if escaped.value != "u":
                raise _JsonSyntax(
                    "invalid_json",
                    line_number=escaped.start.line,
                )
            codepoint, consumed_chars = await self._unicode_escape(escaped.start.line)
            source_chars += consumed_chars
            self._check_value_size(source_chars)
            if 0xD800 <= codepoint <= 0xDBFF:
                slash = await self._take()
                marker = await self._take()
                source_chars += 2
                if (
                    slash is None
                    or marker is None
                    or slash.value != "\\"
                    or marker.value != "u"
                ):
                    raise _JsonSyntax(
                        "invalid_unicode_scalar",
                        line_number=escaped.start.line,
                    )
                low, low_chars = await self._unicode_escape(marker.start.line)
                source_chars += low_chars
                self._check_value_size(source_chars)
                if not 0xDC00 <= low <= 0xDFFF:
                    raise _JsonSyntax(
                        "invalid_unicode_scalar",
                        line_number=marker.start.line,
                    )
                codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
            elif 0xDC00 <= codepoint <= 0xDFFF:
                raise _JsonSyntax(
                    "invalid_unicode_scalar",
                    line_number=escaped.start.line,
                )
            decoded.append(chr(codepoint))
            self._check_value_size(len(decoded))

    async def _unicode_escape(self, line_number: int) -> tuple[int, int]:
        digits: list[str] = []
        for _ in range(4):
            character = await self._take()
            if character is None:
                raise _JsonSyntax("unexpected_eof", line_number=line_number)
            if character.value not in _HEX_DIGITS:
                raise _JsonSyntax("invalid_json", line_number=character.start.line)
            digits.append(character.value)
        return int("".join(digits), 16), 4

    async def _number(self) -> _Token:
        first = await self._peek()
        assert first is not None
        characters: list[str] = []

        async def consume() -> _Character:
            character = await self._take()
            assert character is not None
            characters.append(character.value)
            if len(characters) > self._limits.max_number_chars:
                raise limit_error(
                    adapter_id=self._adapter_id,
                    resource="number_chars",
                    limit=self._limits.max_number_chars,
                )
            self._check_value_size(len(characters))
            return character

        current = await self._peek()
        if current is not None and current.value == "-":
            await consume()
            current = await self._peek()
        if current is None:
            raise _JsonSyntax("unexpected_eof", line_number=first.start.line)
        if current.value == "0":
            last = await consume()
            following = await self._peek()
            if (
                following is not None
                and following.value.isascii()
                and following.value.isdigit()
            ):
                raise _JsonSyntax("invalid_json", line_number=following.start.line)
        elif current.value.isascii() and current.value in "123456789":
            last = await consume()
            while True:
                following = await self._peek()
                if following is None or not (
                    following.value.isascii() and following.value.isdigit()
                ):
                    break
                last = await consume()
        else:
            raise _JsonSyntax("invalid_json", line_number=current.start.line)

        following = await self._peek()
        if following is not None and following.value == ".":
            last = await consume()
            digit = await self._peek()
            if digit is None or not (digit.value.isascii() and digit.value.isdigit()):
                raise _JsonSyntax(
                    "invalid_json",
                    line_number=(digit or last).start.line,
                )
            while True:
                digit = await self._peek()
                if digit is None or not (
                    digit.value.isascii() and digit.value.isdigit()
                ):
                    break
                last = await consume()

        following = await self._peek()
        if following is not None and following.value in {"e", "E"}:
            last = await consume()
            sign = await self._peek()
            if sign is not None and sign.value in {"+", "-"}:
                last = await consume()
            digit = await self._peek()
            if digit is None or not (digit.value.isascii() and digit.value.isdigit()):
                raise _JsonSyntax(
                    "invalid_json",
                    line_number=(digit or last).start.line,
                )
            while True:
                digit = await self._peek()
                if digit is None or not (
                    digit.value.isascii() and digit.value.isdigit()
                ):
                    break
                last = await consume()

        return _Token(
            _TokenKind.NUMBER,
            "".join(characters),
            first.start,
            last.end,
        )

    async def _literal(
        self,
        literal: str,
        kind: _TokenKind,
        value: bool | None,
    ) -> _Token:
        start: _Position | None = None
        end: _Position | None = None
        for source_chars, expected in enumerate(literal, start=1):
            character = await self._take()
            if character is None:
                raise _JsonSyntax(
                    "unexpected_eof",
                    line_number=start.line if start is not None else self.position.line,
                )
            if start is None:
                start = character.start
            if character.value != expected:
                raise _JsonSyntax("invalid_json", line_number=character.start.line)
            self._check_value_size(source_chars)
            end = character.end
        assert start is not None and end is not None
        return _Token(kind, value, start, end)

    def _check_value_size(self, size: int) -> None:
        if size > self._limits.max_value_chars:
            raise limit_error(
                adapter_id=self._adapter_id,
                resource="value_chars",
                limit=self._limits.max_value_chars,
            )


@dataclass(slots=True)
class _ExtractionBudget:
    adapter_id: str
    limits: JsonParserLimits
    max_nesting_depth: int
    max_physical_objects: int
    nodes: int = 0
    keys: int = 0
    physical_objects: int = 0

    def claim_node(self, *, scalar: bool) -> None:
        if self.nodes >= self.limits.max_nodes:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="nodes",
                limit=self.limits.max_nodes,
            )
        physical = 2 if scalar else 1
        if physical > self.max_physical_objects - self.physical_objects:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="physical_objects",
                limit=self.max_physical_objects,
            )
        self.nodes += 1
        self.physical_objects += physical

    def claim_key(self) -> None:
        if self.keys >= self.limits.max_keys:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="keys",
                limit=self.limits.max_keys,
            )
        self.keys += 1

    def claim_continuation_root(self) -> None:
        if self.physical_objects >= self.max_physical_objects:
            raise limit_error(
                adapter_id=self.adapter_id,
                resource="physical_objects",
                limit=self.max_physical_objects,
            )
        self.physical_objects += 1


@dataclass(slots=True)
class _Identifiers:
    node_number: int = 0
    value_number: int = 0

    def node_id(self) -> str:
        value = f"node-{self.node_number}"
        self.node_number += 1
        return value

    def value_id(self) -> str:
        value = f"value-{self.value_number}"
        self.value_number += 1
        return value


@dataclass(frozen=True, slots=True)
class _ParsedTree:
    nodes: tuple[ExtractedTreeNode, ...]
    end: _Position
    node_count: int
    source_chars: int


class _TreeParser:
    """Recursive grammar over bounded depth; output is flat preorder nodes."""

    def __init__(
        self,
        source: SourceArtifact,
        lexer: _JsonLexer,
        limits: JsonParserLimits,
        budget: _ExtractionBudget,
        identifiers: _Identifiers,
        *,
        record_index: int | None,
        record_node_limit: int | None = None,
    ) -> None:
        self._source = source
        self._lexer = lexer
        self._limits = limits
        self._budget = budget
        self._identifiers = identifiers
        self._record_index = record_index
        self._record_node_limit = record_node_limit
        self._record_nodes = 0

    async def parse_value(
        self,
        token: _Token,
        *,
        parent_id: str | None,
        name: str,
        raw_name: str | None,
        order: int,
        pointer: str,
        occurrence_path: tuple[int | None, ...],
        depth: int,
    ) -> _ParsedTree:
        if depth > self._budget.max_nesting_depth:
            raise limit_error(
                adapter_id=self._budget.adapter_id,
                resource="nesting_depth",
                limit=self._budget.max_nesting_depth,
            )
        if len(pointer) > self._limits.max_pointer_chars:
            raise limit_error(
                adapter_id=self._budget.adapter_id,
                resource="json_pointer_chars",
                limit=self._limits.max_pointer_chars,
            )
        scalar = token.kind in {
            _TokenKind.STRING,
            _TokenKind.NUMBER,
            _TokenKind.TRUE,
            _TokenKind.FALSE,
            _TokenKind.NULL,
        }
        if not scalar and token.kind not in {
            _TokenKind.OBJECT_START,
            _TokenKind.ARRAY_START,
        }:
            raise _JsonSyntax("invalid_json", line_number=token.start.line)
        if (
            self._record_node_limit is not None
            and self._record_nodes >= self._record_node_limit
        ):
            raise limit_error(
                adapter_id=self._budget.adapter_id,
                resource="batch_nodes",
                limit=self._record_node_limit,
            )
        self._budget.claim_node(scalar=scalar)
        self._record_nodes += 1
        node_id = self._identifiers.node_id()

        if token.kind is _TokenKind.OBJECT_START:
            return await self._parse_object(
                token,
                node_id=node_id,
                parent_id=parent_id,
                name=name,
                raw_name=raw_name,
                order=order,
                pointer=pointer,
                occurrence_path=occurrence_path,
                depth=depth,
            )
        if token.kind is _TokenKind.ARRAY_START:
            return await self._parse_array(
                token,
                node_id=node_id,
                parent_id=parent_id,
                name=name,
                raw_name=raw_name,
                order=order,
                pointer=pointer,
                occurrence_path=occurrence_path,
                depth=depth,
            )

        value = self._scalar_value(token)
        location = self._location(
            pointer,
            occurrence_path,
            token.start,
            token.end,
        )
        node = ExtractedTreeNode(
            node_id=node_id,
            parent_id=parent_id,
            name=name,
            raw_name=raw_name,
            order=order,
            node_kind=PhysicalNodeKind.SCALAR,
            location=location,
            value=ExtractedValue(
                value_id=self._identifiers.value_id(),
                raw_value=value[0],
                technical_type_hint=value[1],
                location=location,
            ),
        )
        return _ParsedTree((node,), token.end, 1, 0)

    async def _parse_object(
        self,
        opening: _Token,
        *,
        node_id: str,
        parent_id: str | None,
        name: str,
        raw_name: str | None,
        order: int,
        pointer: str,
        occurrence_path: tuple[int | None, ...],
        depth: int,
    ) -> _ParsedTree:
        children: list[ExtractedTreeNode] = []
        occurrences: Counter[str] = Counter()
        member_order = 0
        token = await self._lexer.next()
        if token.kind is _TokenKind.OBJECT_END:
            closing = token
        else:
            while True:
                if token.kind is not _TokenKind.STRING or type(token.value) is not str:
                    raise _JsonSyntax("invalid_json", line_number=token.start.line)
                self._budget.claim_key()
                key = token.value
                separator = await self._lexer.next()
                if separator.kind is not _TokenKind.COLON:
                    raise _JsonSyntax(
                        "invalid_json",
                        line_number=separator.start.line,
                    )
                child_token = await self._lexer.next()
                occurrence = occurrences[key]
                occurrences[key] += 1
                child_pointer = _child_pointer(pointer, key)
                child = await self.parse_value(
                    child_token,
                    parent_id=node_id,
                    name="member",
                    raw_name=key,
                    order=member_order,
                    pointer=child_pointer,
                    occurrence_path=(*occurrence_path, occurrence),
                    depth=depth + 1,
                )
                children.extend(child.nodes)
                member_order += 1
                delimiter = await self._lexer.next()
                if delimiter.kind is _TokenKind.OBJECT_END:
                    closing = delimiter
                    break
                if delimiter.kind is not _TokenKind.COMMA:
                    raise _JsonSyntax(
                        "invalid_json",
                        line_number=delimiter.start.line,
                    )
                token = await self._lexer.next()

        location = self._location(
            pointer,
            occurrence_path,
            opening.start,
            closing.end,
        )
        root = ExtractedTreeNode(
            node_id=node_id,
            parent_id=parent_id,
            name=name,
            raw_name=raw_name,
            order=order,
            node_kind=PhysicalNodeKind.OBJECT,
            location=location,
        )
        return _ParsedTree(
            (root, *children),
            closing.end,
            1 + len(children),
            0,
        )

    async def _parse_array(
        self,
        opening: _Token,
        *,
        node_id: str,
        parent_id: str | None,
        name: str,
        raw_name: str | None,
        order: int,
        pointer: str,
        occurrence_path: tuple[int | None, ...],
        depth: int,
    ) -> _ParsedTree:
        children: list[ExtractedTreeNode] = []
        item_index = 0
        token = await self._lexer.next()
        if token.kind is _TokenKind.ARRAY_END:
            closing = token
        else:
            while True:
                if item_index >= self._limits.max_array_items:
                    raise limit_error(
                        adapter_id=self._budget.adapter_id,
                        resource="array_items",
                        limit=self._limits.max_array_items,
                    )
                child = await self.parse_value(
                    token,
                    parent_id=node_id,
                    name="item",
                    raw_name=None,
                    order=item_index,
                    pointer=_child_pointer(pointer, str(item_index)),
                    occurrence_path=(*occurrence_path, None),
                    depth=depth + 1,
                )
                children.extend(child.nodes)
                item_index += 1
                delimiter = await self._lexer.next()
                if delimiter.kind is _TokenKind.ARRAY_END:
                    closing = delimiter
                    break
                if delimiter.kind is not _TokenKind.COMMA:
                    raise _JsonSyntax(
                        "invalid_json",
                        line_number=delimiter.start.line,
                    )
                token = await self._lexer.next()

        location = self._location(
            pointer,
            occurrence_path,
            opening.start,
            closing.end,
        )
        root = ExtractedTreeNode(
            node_id=node_id,
            parent_id=parent_id,
            name=name,
            raw_name=raw_name,
            order=order,
            node_kind=PhysicalNodeKind.ARRAY,
            location=location,
        )
        return _ParsedTree(
            (root, *children),
            closing.end,
            1 + len(children),
            0,
        )

    def _location(
        self,
        pointer: str,
        occurrence_path: tuple[int | None, ...],
        start: _Position,
        end: _Position,
    ) -> JsonPointerLocation:
        return JsonPointerLocation(
            source=self._source.ref,
            pointer=pointer,
            occurrence_path=occurrence_path,
            record_index=self._record_index,
            line_start=start.line,
            line_end=end.line,
            column_start=start.column,
            column_end=end.column,
        )

    @staticmethod
    def _scalar_value(
        token: _Token,
    ) -> tuple[StringScalar | BooleanScalar | NullScalar, str | None]:
        if token.kind is _TokenKind.STRING:
            assert type(token.value) is str
            return StringScalar(value=token.value), None
        if token.kind is _TokenKind.NUMBER:
            assert type(token.value) is str
            hint = (
                "json.number"
                if any(marker in token.value for marker in ".eE")
                else "json.integer"
            )
            return StringScalar(value=token.value), hint
        if token.kind is _TokenKind.TRUE:
            return BooleanScalar(value=True), None
        if token.kind is _TokenKind.FALSE:
            return BooleanScalar(value=False), None
        assert token.kind is _TokenKind.NULL
        return NullScalar(), None


def _child_pointer(parent: str, segment: str) -> str:
    escaped = segment.replace("~", "~0").replace("/", "~1")
    return f"{parent}/{escaped}"


def _malformed(
    reason: str,
    *,
    line_number: int,
    record_number: int | None = None,
) -> ParserError:
    details: dict[str, str | int] = {
        "reason": reason,
        "line_number": line_number,
    }
    if record_number is not None:
        details["record_number"] = record_number
    return ParserError(
        error_code="PARSER_MALFORMED_INPUT",
        message="JSON source содержит некорректную физическую запись.",
        details=details,
    )


async def _parse_complete_record(
    source: SourceArtifact,
    text: str,
    limits: JsonParserLimits,
    budget: _ExtractionBudget,
    identifiers: _Identifiers,
    *,
    adapter_id: str,
    record_index: int | None,
    line_number: int,
    enforce_batch_limit: bool = True,
) -> _ParsedTree:
    characters = _StringCharacters(text, line_number=line_number)
    lexer = _JsonLexer(characters, limits, adapter_id=adapter_id)
    lexer.begin_record()
    first = await lexer.next()
    parser = _TreeParser(
        source,
        lexer,
        limits,
        budget,
        identifiers,
        record_index=record_index,
        record_node_limit=limits.max_batch_nodes if enforce_batch_limit else None,
    )
    tree = await parser.parse_value(
        first,
        parent_id=None,
        name="root",
        raw_name=None,
        order=0,
        pointer="",
        occurrence_path=(),
        depth=1,
    )
    trailing = await lexer.next()
    if trailing.kind is not _TokenKind.EOF:
        raise _JsonSyntax("trailing_content", line_number=trailing.start.line)
    source_chars = lexer.end_record(lexer.position)
    return _ParsedTree(tree.nodes, tree.end, tree.node_count, source_chars)


async def _read_probe_text(
    source: SourceArtifact,
    context: ProbeContext,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
) -> tuple[str, str | None, bool]:
    target = min(
        source.size_bytes,
        context.max_probe_bytes,
        limits.max_probe_bytes,
    )
    sample = bytearray()
    offset = 0
    while offset < target:
        chunk = await _checked_read(
            context.reader,
            offset=offset,
            size=min(limits.read_chunk_bytes, target - offset),
        )
        if not chunk:
            raise _malformed("unexpected_eof", line_number=1)
        sample.extend(chunk)
        offset += len(chunk)
        await asyncio.sleep(0)
    raw = bytes(sample)
    if has_binary_container_signature(raw):
        return "", None, target == source.size_bytes
    reported = "utf-8-sig" if raw.startswith(codecs.BOM_UTF8) else "utf-8"
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="strict")
        text = decoder.decode(raw, final=target == source.size_bytes)
    except UnicodeDecodeError as error:
        # Ошибка UTF-8 чужого формата не должна отменять выбор TXT/CSV/XML.
        # Для подтверждённого JSON prefix сохраняется прежний typed отказ.
        prefix = error.object[: error.start].decode("utf-8-sig")
        if not await _has_json_probe_prefix(source, prefix, limits, adapter_id):
            return "", None, target == source.size_bytes
        raise encoding_error("decode_failed", encoding=reported) from None
    return text, reported, target == source.size_bytes


async def _has_json_probe_prefix(
    source: SourceArtifact,
    prefix: str,
    limits: JsonParserLimits,
    adapter_id: str,
) -> bool:
    if _first_non_whitespace(prefix) is None:
        return False
    budget = _ExtractionBudget(
        adapter_id,
        limits,
        limits.max_nesting_depth,
        max(2, limits.max_nodes * 2),
    )
    try:
        await _parse_complete_record(
            source,
            prefix,
            limits,
            budget,
            _Identifiers(),
            adapter_id=adapter_id,
            record_index=None,
            line_number=1,
            enforce_batch_limit=False,
        )
    except _JsonSyntax as error:
        if error.reason == "unexpected_eof":
            return True
        return (
            await _valid_json_lines_in_sample(
                source,
                prefix,
                limits,
                adapter_id=adapter_id,
                complete=False,
                raise_after_candidate=False,
            )
            >= 2
        )
    return True


def _sample_physical_lines(
    text: str,
    *,
    complete: bool,
) -> tuple[tuple[int, str], ...]:
    lines: list[tuple[int, str]] = []
    start = 0
    line_number = 1
    position = 0
    while position < len(text):
        character = text[position]
        if character not in {"\r", "\n"}:
            position += 1
            continue
        lines.append((line_number, text[start:position]))
        if (
            character == "\r"
            and position + 1 < len(text)
            and text[position + 1] == "\n"
        ):
            position += 1
        position += 1
        start = position
        line_number += 1
    if start < len(text) and complete:
        lines.append((line_number, text[start:]))
    return tuple(lines)


def _first_non_whitespace(text: str) -> str | None:
    return next(
        (character for character in text if character not in _JSON_WHITESPACE), None
    )


async def _valid_json_lines_in_sample(
    source: SourceArtifact,
    text: str,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
    complete: bool,
    raise_after_candidate: bool,
) -> int:
    valid = 0
    for line_number, line in _sample_physical_lines(text, complete=complete):
        if _first_non_whitespace(line) is None:
            continue
        if valid >= limits.max_probe_records:
            break
        budget = _ExtractionBudget(
            adapter_id,
            limits,
            limits.max_nesting_depth,
            max(2, limits.max_nodes * 2),
        )
        try:
            await _parse_complete_record(
                source,
                line,
                limits,
                budget,
                _Identifiers(),
                adapter_id=adapter_id,
                record_index=valid,
                line_number=line_number,
                enforce_batch_limit=False,
            )
        except _JsonSyntax as error:
            if (
                raise_after_candidate
                and valid >= 2
                and _first_non_whitespace(line) in _JSON_TOKEN_STARTS
            ):
                raise _malformed(
                    error.reason,
                    line_number=line_number,
                    record_number=valid + 1,
                ) from None
            return valid
        valid += 1
    return valid


async def probe_json_document(
    source: SourceArtifact,
    context: ProbeContext,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
    version: str,
) -> ProbeResult:
    text, encoding, complete = await _read_probe_text(
        source, context, limits, adapter_id=adapter_id
    )
    supported = False
    if source.size_bytes > 0 and _first_non_whitespace(text) is not None:
        json_lines = await _valid_json_lines_in_sample(
            source,
            text,
            limits,
            adapter_id=adapter_id,
            complete=complete,
            raise_after_candidate=False,
        )
        if json_lines < 2:
            budget = _ExtractionBudget(
                adapter_id,
                limits,
                limits.max_nesting_depth,
                max(2, limits.max_nodes * 2),
            )
            try:
                await _parse_complete_record(
                    source,
                    text,
                    limits,
                    budget,
                    _Identifiers(),
                    adapter_id=adapter_id,
                    record_index=None,
                    line_number=1,
                    enforce_batch_limit=False,
                )
            except _JsonSyntax as error:
                first = _first_non_whitespace(text)
                if (
                    not complete
                    and error.reason == "unexpected_eof"
                    and first in {"{", "["}
                ):
                    supported = True
            else:
                supported = complete

    content = ProbeSignal(
        kind=ProbeSignalKind.CONTENT_MEDIA_TYPE,
        outcome=(
            ProbeSignalOutcome.MATCH if supported else ProbeSignalOutcome.INCONCLUSIVE
        ),
    )
    structure = ProbeSignal(
        kind=ProbeSignalKind.INTERNAL_STRUCTURE,
        outcome=(
            ProbeSignalOutcome.MATCH if supported else ProbeSignalOutcome.INCONCLUSIVE
        ),
    )
    advisory = advisory_signals(
        source,
        media_types=JSON_MEDIA_TYPES,
        extensions=JSON_EXTENSIONS,
    )
    return ProbeResult(
        source=source.ref,
        adapter_id=adapter_id,
        adapter_version=version,
        supported=supported,
        confidence=Decimal("1") if supported else Decimal("0"),
        detected_media_type="application/json" if supported else None,
        detected_encoding=encoding,
        format_id="json" if supported else None,
        signals=(content, structure, *advisory),
    )


async def probe_json_lines(
    source: SourceArtifact,
    context: ProbeContext,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
    version: str,
) -> ProbeResult:
    text, encoding, complete = await _read_probe_text(
        source, context, limits, adapter_id=adapter_id
    )
    valid = await _valid_json_lines_in_sample(
        source,
        text,
        limits,
        adapter_id=adapter_id,
        complete=complete,
        raise_after_candidate=True,
    )
    supported = valid >= 2
    content = ProbeSignal(
        kind=ProbeSignalKind.CONTENT_MEDIA_TYPE,
        outcome=(
            ProbeSignalOutcome.MATCH if supported else ProbeSignalOutcome.INCONCLUSIVE
        ),
    )
    structure = ProbeSignal(
        kind=ProbeSignalKind.INTERNAL_STRUCTURE,
        outcome=(
            ProbeSignalOutcome.MATCH if supported else ProbeSignalOutcome.INCONCLUSIVE
        ),
    )
    advisory = advisory_signals(
        source,
        media_types=JSON_LINES_MEDIA_TYPES,
        extensions=JSON_LINES_EXTENSIONS,
    )
    return ProbeResult(
        source=source.ref,
        adapter_id=adapter_id,
        adapter_version=version,
        supported=supported,
        confidence=Decimal("1") if supported else Decimal("0"),
        detected_media_type="application/x-ndjson" if supported else None,
        detected_encoding=encoding,
        format_id="jsonl" if supported else None,
        signals=(content, structure, *advisory),
    )


def _parser_options_fingerprint(
    limits: JsonParserLimits,
    context: ParseContext,
) -> str:
    return canonical_sha256_value(
        cast(
            CanonicalInput,
            {
                "batch_options": asdict(context.batch_options),
                "limits": asdict(limits),
            },
        )
    )


def _bounded_indexed_refs(
    *,
    run_id: str,
    batch_index: int,
    trees: tuple[ExtractedTreeNode, ...],
    limit: int,
) -> tuple[PhysicalSourceRef, ...]:
    refs: list[PhysicalSourceRef] = []
    for node in trees:
        if len(refs) >= limit:
            return tuple(refs)
        refs.append(
            PhysicalSourceRef(
                extraction_id=run_id,
                batch_index=batch_index,
                kind=PhysicalObjectKind.TREE_NODE,
                local_id=node.node_id,
            )
        )
    for node in trees:
        if node.value is None or node.value.value_id is None:
            continue
        if len(refs) >= limit:
            return tuple(refs)
        refs.append(
            PhysicalSourceRef(
                extraction_id=run_id,
                batch_index=batch_index,
                kind=PhysicalObjectKind.VALUE,
                local_id=node.value.value_id,
            )
        )
    return tuple(refs)


def _build_batch(
    *,
    source: SourceArtifact,
    run_id: str,
    adapter_id: str,
    version: str,
    batch_index: int,
    trees: tuple[ExtractedTreeNode, ...],
    record_count: int,
    prior_summaries: tuple[ExtractedBatchSummary, ...],
    prior_indexed_refs: Sequence[PhysicalSourceRef],
    options_fingerprint: str,
    is_last: bool,
) -> ExtractedBatch:
    indexed_refs = _bounded_indexed_refs(
        run_id=run_id,
        batch_index=batch_index,
        trees=trees,
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
        trees=trees,
        record_count=record_count,
        indexed_refs=indexed_refs,
    )
    fingerprint = batch_fingerprint(draft.model_copy(update={"is_last": is_last}))
    summary = draft.model_copy(update={"batch_fingerprint": fingerprint}).to_summary()
    manifest = None
    if is_last:
        summaries = (*prior_summaries, summary)
        seed = ExtractedDatasetManifest(
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
        manifest = seed.model_copy(
            update={"extraction_fingerprint": manifest_fingerprint(seed)}
        )
    return ExtractedBatch(
        schema_version=_SCHEMA_VERSION,
        extraction_id=run_id,
        batch_index=batch_index,
        source=source.ref,
        parser_id=adapter_id,
        parser_version=version,
        batch_fingerprint=fingerprint,
        trees=trees,
        record_count=record_count,
        indexed_refs=indexed_refs,
        is_last=is_last,
        manifest=manifest,
    )


def _ensure_batch_slot(
    context: ParseContext,
    *,
    adapter_id: str,
    batch_index: int,
    is_last: bool,
) -> None:
    if batch_index >= context.batch_options.max_batches or (
        not is_last and batch_index + 1 >= context.batch_options.max_batches
    ):
        raise limit_error(
            adapter_id=adapter_id,
            resource="batch_count",
            limit=context.batch_options.max_batches,
        )


@dataclass(frozen=True, slots=True)
class _SourceLine:
    number: int
    text: str
    is_source_last: bool


async def _source_lines(
    characters: _SourceCharacters,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
) -> AsyncIterator[_SourceLine]:
    pending_number: int | None = None
    pending_text: str | None = None
    line_number = 1
    buffer: list[str] = []
    has_content = False
    while True:
        character = await characters.take()
        if character is None:
            if has_content:
                if pending_number is not None and pending_text is not None:
                    yield _SourceLine(pending_number, pending_text, False)
                yield _SourceLine(line_number, "".join(buffer), True)
            elif pending_number is not None and pending_text is not None:
                yield _SourceLine(pending_number, pending_text, True)
            return
        if character.value not in {"\r", "\n"}:
            buffer.append(character.value)
            if len(buffer) > limits.max_record_chars:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="record_chars",
                    limit=limits.max_record_chars,
                )
            if character.value in _JSON_WHITESPACE or has_content:
                continue
            has_content = True
            if pending_number is not None and pending_text is not None:
                yield _SourceLine(pending_number, pending_text, False)
                pending_number = None
                pending_text = None
            continue
        if character.value == "\r":
            following = await characters.peek()
            if following is not None and following.value == "\n":
                await characters.take()
        if has_content:
            if pending_number is not None and pending_text is not None:
                yield _SourceLine(pending_number, pending_text, False)
            pending_number = line_number
            pending_text = "".join(buffer)
        buffer.clear()
        has_content = False
        line_number += 1


def _physical_count(nodes: Sequence[ExtractedTreeNode]) -> int:
    return len(nodes) + sum(node.value is not None for node in nodes)


async def stream_json_lines(
    source: SourceArtifact,
    context: ParseContext,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
    version: str,
) -> AsyncIterator[ExtractedBatch]:
    characters = _SourceCharacters(
        source,
        context,
        limits,
        adapter_id=adapter_id,
    )
    options_fingerprint = _parser_options_fingerprint(limits, context)
    run_id = extraction_id(
        source,
        adapter_id=adapter_id,
        version=version,
        options_fingerprint=options_fingerprint,
    )
    budget = _ExtractionBudget(
        adapter_id,
        limits,
        min(limits.max_nesting_depth, context.max_nesting_depth),
        context.max_physical_objects,
    )
    identifiers = _Identifiers()
    summaries: list[ExtractedBatchSummary] = []
    indexed_refs: list[PhysicalSourceRef] = []
    pending: list[ExtractedTreeNode] = []
    pending_records = 0
    pending_chars = 0
    total_records = 0
    batch_index = 0
    saw_record = False

    async for line in _source_lines(characters, limits, adapter_id=adapter_id):
        if _first_non_whitespace(line.text) is None:
            continue
        saw_record = True
        if total_records >= context.max_records:
            raise limit_error(
                adapter_id=adapter_id,
                resource="records",
                limit=context.max_records,
            )
        try:
            tree = await _parse_complete_record(
                source,
                line.text,
                limits,
                budget,
                identifiers,
                adapter_id=adapter_id,
                record_index=total_records,
                line_number=line.number,
            )
        except _JsonSyntax as error:
            raise _malformed(
                error.reason,
                line_number=line.number,
                record_number=total_records + 1,
            ) from None
        if (
            tree.node_count > limits.max_batch_nodes
            or tree.source_chars > limits.max_batch_chars
        ):
            resource = (
                "batch_nodes"
                if tree.node_count > limits.max_batch_nodes
                else "batch_chars"
            )
            limit = (
                limits.max_batch_nodes
                if resource == "batch_nodes"
                else limits.max_batch_chars
            )
            raise limit_error(adapter_id=adapter_id, resource=resource, limit=limit)

        would_overflow = pending and (
            pending_records >= context.batch_options.batch_size
            or len(pending) + tree.node_count > limits.max_batch_nodes
            or pending_chars + tree.source_chars > limits.max_batch_chars
        )
        if would_overflow:
            _ensure_batch_slot(
                context,
                adapter_id=adapter_id,
                batch_index=batch_index,
                is_last=False,
            )
            batch = _build_batch(
                source=source,
                run_id=run_id,
                adapter_id=adapter_id,
                version=version,
                batch_index=batch_index,
                trees=tuple(pending),
                record_count=pending_records,
                prior_summaries=tuple(summaries),
                prior_indexed_refs=indexed_refs,
                options_fingerprint=options_fingerprint,
                is_last=False,
            )
            summaries.append(batch.to_summary())
            indexed_refs.extend(batch.indexed_refs)
            yield batch
            pending.clear()
            pending_records = 0
            pending_chars = 0
            batch_index += 1
            await asyncio.sleep(0)

        pending.extend(tree.nodes)
        pending_records += 1
        pending_chars += tree.source_chars
        total_records += 1
        full = (
            pending_records >= context.batch_options.batch_size
            or len(pending) >= limits.max_batch_nodes
            or pending_chars >= limits.max_batch_chars
        )
        if not full and not line.is_source_last:
            continue
        _ensure_batch_slot(
            context,
            adapter_id=adapter_id,
            batch_index=batch_index,
            is_last=line.is_source_last,
        )
        batch = _build_batch(
            source=source,
            run_id=run_id,
            adapter_id=adapter_id,
            version=version,
            batch_index=batch_index,
            trees=tuple(pending),
            record_count=pending_records,
            prior_summaries=tuple(summaries),
            prior_indexed_refs=indexed_refs,
            options_fingerprint=options_fingerprint,
            is_last=line.is_source_last,
        )
        if not line.is_source_last:
            summaries.append(batch.to_summary())
            indexed_refs.extend(batch.indexed_refs)
        yield batch
        if line.is_source_last:
            return
        pending.clear()
        pending_records = 0
        pending_chars = 0
        batch_index += 1
        await asyncio.sleep(0)

    if not saw_record:
        raise _malformed("invalid_json", line_number=1, record_number=1)
    _ensure_batch_slot(
        context,
        adapter_id=adapter_id,
        batch_index=batch_index,
        is_last=True,
    )
    yield _build_batch(
        source=source,
        run_id=run_id,
        adapter_id=adapter_id,
        version=version,
        batch_index=batch_index,
        trees=tuple(pending),
        record_count=pending_records,
        prior_summaries=tuple(summaries),
        prior_indexed_refs=indexed_refs,
        options_fingerprint=options_fingerprint,
        is_last=True,
    )


def _array_root(
    source: SourceArtifact,
    *,
    root_id: str,
    segment_index: int,
    child_start_index: int,
    child_count: int,
    is_last: bool,
) -> ExtractedTreeNode:
    return ExtractedTreeNode(
        node_id=root_id,
        name="root",
        order=0,
        node_kind=PhysicalNodeKind.ARRAY,
        location=JsonPointerLocation(
            source=source.ref,
            pointer="",
            occurrence_path=(),
        ),
        tree_id="tree-0",
        segment_index=segment_index,
        child_start_index=child_start_index,
        child_count=child_count,
        is_last_segment=is_last,
    )


async def stream_json_document(
    source: SourceArtifact,
    context: ParseContext,
    limits: JsonParserLimits,
    *,
    adapter_id: str,
    version: str,
) -> AsyncIterator[ExtractedBatch]:
    characters = _SourceCharacters(
        source,
        context,
        limits,
        adapter_id=adapter_id,
    )
    lexer = _JsonLexer(characters, limits, adapter_id=adapter_id)
    options_fingerprint = _parser_options_fingerprint(limits, context)
    run_id = extraction_id(
        source,
        adapter_id=adapter_id,
        version=version,
        options_fingerprint=options_fingerprint,
    )
    budget = _ExtractionBudget(
        adapter_id,
        limits,
        min(limits.max_nesting_depth, context.max_nesting_depth),
        context.max_physical_objects,
    )
    identifiers = _Identifiers()
    lexer.begin_record()
    try:
        first = await lexer.next()
        if first.kind is not _TokenKind.ARRAY_START:
            if context.max_records < 1:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="records",
                    limit=context.max_records,
                )
            parser = _TreeParser(
                source,
                lexer,
                limits,
                budget,
                identifiers,
                record_index=None,
                record_node_limit=limits.max_batch_nodes,
            )
            tree = await parser.parse_value(
                first,
                parent_id=None,
                name="root",
                raw_name=None,
                order=0,
                pointer="",
                occurrence_path=(),
                depth=1,
            )
            trailing = await lexer.next()
            if trailing.kind is not _TokenKind.EOF:
                raise _JsonSyntax("trailing_content", line_number=trailing.start.line)
            source_chars = lexer.end_record(lexer.position)
            if (
                tree.node_count > limits.max_batch_nodes
                or source_chars > limits.max_batch_chars
            ):
                resource = (
                    "batch_nodes"
                    if tree.node_count > limits.max_batch_nodes
                    else "batch_chars"
                )
                limit = (
                    limits.max_batch_nodes
                    if resource == "batch_nodes"
                    else limits.max_batch_chars
                )
                raise limit_error(adapter_id=adapter_id, resource=resource, limit=limit)
            return_batch = _build_batch(
                source=source,
                run_id=run_id,
                adapter_id=adapter_id,
                version=version,
                batch_index=0,
                trees=tree.nodes,
                record_count=1,
                prior_summaries=(),
                prior_indexed_refs=(),
                options_fingerprint=options_fingerprint,
                is_last=True,
            )
            yield return_batch
            return

        lexer.abandon_record()
        if budget.max_nesting_depth < 1:
            raise limit_error(
                adapter_id=adapter_id,
                resource="nesting_depth",
                limit=budget.max_nesting_depth,
            )
        budget.claim_node(scalar=False)
        root_id = identifiers.node_id()
        lexer.begin_record()
        token = await lexer.next()
        if token.kind is _TokenKind.ARRAY_END:
            lexer.abandon_record()
            trailing = await lexer.next()
            if trailing.kind is not _TokenKind.EOF:
                raise _JsonSyntax("trailing_content", line_number=trailing.start.line)
            root = _array_root(
                source,
                root_id=root_id,
                segment_index=0,
                child_start_index=0,
                child_count=0,
                is_last=True,
            )
            yield _build_batch(
                source=source,
                run_id=run_id,
                adapter_id=adapter_id,
                version=version,
                batch_index=0,
                trees=(root,),
                record_count=0,
                prior_summaries=(),
                prior_indexed_refs=(),
                options_fingerprint=options_fingerprint,
                is_last=True,
            )
            return

        summaries: list[ExtractedBatchSummary] = []
        indexed_refs: list[PhysicalSourceRef] = []
        pending: list[ExtractedTreeNode] = []
        pending_chars = 0
        pending_records = 0
        total_records = 0
        batch_index = 0
        segment_start = 0
        while True:
            if total_records >= context.max_records:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="records",
                    limit=context.max_records,
                )
            if total_records >= limits.max_array_items:
                raise limit_error(
                    adapter_id=adapter_id,
                    resource="array_items",
                    limit=limits.max_array_items,
                )
            parser = _TreeParser(
                source,
                lexer,
                limits,
                budget,
                identifiers,
                record_index=None,
                record_node_limit=limits.max_batch_nodes,
            )
            tree = await parser.parse_value(
                token,
                parent_id=root_id,
                name="item",
                raw_name=None,
                order=total_records,
                pointer=f"/{total_records}",
                occurrence_path=(None,),
                depth=2,
            )
            delimiter, source_chars = await lexer.next_after_record()
            if delimiter.kind not in {_TokenKind.COMMA, _TokenKind.ARRAY_END}:
                raise _JsonSyntax("invalid_json", line_number=delimiter.start.line)
            if (
                tree.node_count + 1 > limits.max_batch_nodes
                or source_chars > limits.max_batch_chars
            ):
                resource = (
                    "batch_nodes"
                    if tree.node_count + 1 > limits.max_batch_nodes
                    else "batch_chars"
                )
                limit = (
                    limits.max_batch_nodes
                    if resource == "batch_nodes"
                    else limits.max_batch_chars
                )
                raise limit_error(adapter_id=adapter_id, resource=resource, limit=limit)
            would_overflow = pending and (
                len(pending) + tree.node_count + 1 > limits.max_batch_nodes
                or pending_chars + source_chars > limits.max_batch_chars
            )
            if would_overflow:
                if batch_index > 0:
                    budget.claim_continuation_root()
                _ensure_batch_slot(
                    context,
                    adapter_id=adapter_id,
                    batch_index=batch_index,
                    is_last=False,
                )
                root = _array_root(
                    source,
                    root_id=root_id,
                    segment_index=batch_index,
                    child_start_index=segment_start,
                    child_count=pending_records,
                    is_last=False,
                )
                batch = _build_batch(
                    source=source,
                    run_id=run_id,
                    adapter_id=adapter_id,
                    version=version,
                    batch_index=batch_index,
                    trees=(root, *pending),
                    record_count=pending_records,
                    prior_summaries=tuple(summaries),
                    prior_indexed_refs=indexed_refs,
                    options_fingerprint=options_fingerprint,
                    is_last=False,
                )
                summaries.append(batch.to_summary())
                indexed_refs.extend(batch.indexed_refs)
                yield batch
                pending.clear()
                pending_chars = 0
                pending_records = 0
                batch_index += 1
                segment_start = total_records
                await asyncio.sleep(0)

            pending.extend(tree.nodes)
            pending_chars += source_chars
            pending_records += 1
            total_records += 1
            is_last = delimiter.kind is _TokenKind.ARRAY_END
            if is_last:
                trailing = await lexer.next()
                if trailing.kind is not _TokenKind.EOF:
                    raise _JsonSyntax(
                        "trailing_content",
                        line_number=trailing.start.line,
                    )
            full = pending_records >= context.batch_options.batch_size
            if not full and not is_last:
                lexer.begin_record()
                token = await lexer.next()
                continue

            if batch_index > 0:
                budget.claim_continuation_root()
            _ensure_batch_slot(
                context,
                adapter_id=adapter_id,
                batch_index=batch_index,
                is_last=is_last,
            )
            root = _array_root(
                source,
                root_id=root_id,
                segment_index=batch_index,
                child_start_index=segment_start,
                child_count=pending_records,
                is_last=is_last,
            )
            batch = _build_batch(
                source=source,
                run_id=run_id,
                adapter_id=adapter_id,
                version=version,
                batch_index=batch_index,
                trees=(root, *pending),
                record_count=pending_records,
                prior_summaries=tuple(summaries),
                prior_indexed_refs=indexed_refs,
                options_fingerprint=options_fingerprint,
                is_last=is_last,
            )
            if not is_last:
                summaries.append(batch.to_summary())
                indexed_refs.extend(batch.indexed_refs)
            yield batch
            if is_last:
                return
            pending.clear()
            pending_chars = 0
            pending_records = 0
            batch_index += 1
            segment_start = total_records
            await asyncio.sleep(0)
            lexer.begin_record()
            token = await lexer.next()
    except _JsonSyntax as error:
        raise _malformed(
            error.reason,
            line_number=error.line_number,
        ) from None


__all__ = (
    "JSON_EXTENSIONS",
    "JSON_LINES_EXTENSIONS",
    "JSON_LINES_MEDIA_TYPES",
    "JSON_MEDIA_TYPES",
    "JsonParserLimits",
    "probe_json_document",
    "probe_json_lines",
    "stream_json_document",
    "stream_json_lines",
)
