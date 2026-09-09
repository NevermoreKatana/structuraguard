"""SafeLoader events без constructors, implicit coercion и alias expansion."""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.source import (
    ExtensionLocation,
    ExtractedBatch,
    ExtractedTreeNode,
    ExtractedValue,
    PhysicalMetadataEntry,
    PhysicalNodeKind,
    ProbeResult,
    SourceArtifact,
)
from structuraguard.ports.source import ParseContext, ProbeContext

from ._markup import (
    Budget,
    MarkupDocument,
    MarkupLimits,
    MarkupUnit,
    malformed,
    markup_batches,
    missing_extra,
    probe_bytes,
    probe_result,
    rejected,
    utf8_chunks,
)

_SAFE_TAGS = frozenset(
    "tag:yaml.org,2002:" + name
    for name in (
        "str",
        "null",
        "bool",
        "int",
        "float",
        "binary",
        "timestamp",
        "map",
        "seq",
        "set",
        "omap",
        "pairs",
        "merge",
        "value",
        "yaml",
    )
)


@dataclass(frozen=True, slots=True, kw_only=True)
class YamlParserLimits(MarkupLimits):
    """Documents ограничены до SafeLoader; aliases остаются ссылками, не копиями."""

    max_document_chars: int = 4_000_000
    max_documents: int = 10000
    max_aliases: int = 1000
    max_anchors: int = 1000

    def __post_init__(self) -> None:
        MarkupLimits.__post_init__(self)
        for name, cap, minimum in (
            ("max_document_chars", 32_000_000, 1),
            ("max_documents", 100000, 1),
            ("max_aliases", 100000, 0),
            ("max_anchors", 100000, 0),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= cap:
                raise ValueError(f"{name} должен быть int от {minimum} до {cap}")


@dataclass(slots=True)
class _Frame:
    index: int
    path: str
    child_count: int = 0


async def _document(
    text: str,
    *,
    source: SourceArtifact,
    budget: Budget,
    limits: YamlParserLimits,
    document_index: int,
    line_offset: int,
    allow_incomplete: bool = False,
) -> MarkupUnit:
    try:
        from yaml import SafeLoader, YAMLError
        from yaml.events import (
            AliasEvent,
            CollectionEndEvent,
            CollectionStartEvent,
            DocumentStartEvent,
            MappingStartEvent,
            ScalarEvent,
            SequenceStartEvent,
        )
    except ImportError:
        raise missing_extra("yaml") from None
    try:
        loader = SafeLoader(text)
    except YAMLError as error:
        position = getattr(error, "position", 0)
        prefix = text[:position] if type(position) is int else ""
        line = prefix.replace("\r\n", "\n").replace("\r", "\n").count("\n")
        raise malformed("invalid_yaml", line_number=line_offset + line + 1) from None
    # types-PyYAML оставляет get_event untyped; сужаем только эту backend boundary.
    next_event = cast(Callable[[], object], loader.get_event)
    trees: list[ExtractedTreeNode] = []
    stack: list[_Frame] = []
    anchors: dict[str, str] = {}
    aliases = documents = chars = 0
    try:
        while loader.check_event():
            await asyncio.sleep(0)
            event = next_event()
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise malformed("invalid_yaml", line_number=line_offset + 1)
            if isinstance(event, CollectionEndEvent):
                frame = stack.pop()
                node = trees[frame.index]
                location = node.location
                assert isinstance(location, ExtensionLocation)
                if event.end_mark is not None:
                    entries = tuple(
                        e
                        for e in location.metadata
                        if e.key not in {"line_end", "column_end"}
                    )
                    location = location.model_copy(
                        update={
                            "metadata": (
                                *entries,
                                PhysicalMetadataEntry(
                                    key="line_end",
                                    value=line_offset + event.end_mark.line + 1,
                                ),
                                PhysicalMetadataEntry(
                                    key="column_end", value=event.end_mark.column
                                ),
                            )
                        }
                    )
                    trees[frame.index] = node.model_copy(update={"location": location})
                continue
            if not isinstance(event, AliasEvent | ScalarEvent | CollectionStartEvent):
                continue
            if event.start_mark is None or event.end_mark is None:
                raise malformed("invalid_yaml", line_number=line_offset + 1)
            budget.node(depth=len(stack) + 1)
            budget.check("subtree_nodes", len(trees) + 1, limits.max_subtree_nodes)
            parent = stack[-1] if stack else None
            order = parent.child_count if parent else 0
            path = f"{parent.path}/{order}" if parent else ""
            budget.check("yaml_path_chars", len(path), 4096)
            location = ExtensionLocation(
                source=source.ref,
                namespace="yaml:mark",
                metadata=(
                    PhysicalMetadataEntry(key="document_index", value=document_index),
                    PhysicalMetadataEntry(key="path", value=path),
                    PhysicalMetadataEntry(
                        key="line_start", value=line_offset + event.start_mark.line + 1
                    ),
                    PhysicalMetadataEntry(
                        key="column_start", value=event.start_mark.column
                    ),
                    PhysicalMetadataEntry(
                        key="line_end", value=line_offset + event.end_mark.line + 1
                    ),
                    PhysicalMetadataEntry(
                        key="column_end", value=event.end_mark.column
                    ),
                ),
            )
            metadata: list[PhysicalMetadataEntry] = []
            if parent and trees[parent.index].node_kind is PhysicalNodeKind.MAPPING:
                metadata.extend(
                    (
                        PhysicalMetadataEntry(
                            key="mapping_role",
                            value="key" if order % 2 == 0 else "value",
                        ),
                        PhysicalMetadataEntry(key="pair_index", value=order // 2),
                    )
                )
            kind = PhysicalNodeKind.SCALAR
            value: ExtractedValue | None = None
            raw = event.value if isinstance(event, ScalarEvent) else ""
            budget.check("value_chars", len(raw), limits.max_value_chars)
            if any(0xD800 <= ord(character) <= 0xDFFF for character in raw):
                try:
                    raw = raw.encode("utf-16-le", "surrogatepass").decode(
                        "utf-16-le", "strict"
                    )
                except UnicodeDecodeError:
                    raise malformed(
                        "invalid_unicode_scalar",
                        line_number=line_offset + event.start_mark.line + 1,
                    ) from None
            if isinstance(event, AliasEvent):
                aliases += 1
                budget.check("aliases", aliases, limits.max_aliases)
                if event.anchor not in anchors:
                    raise malformed(
                        "invalid_yaml_alias",
                        line_number=line_offset + event.start_mark.line + 1,
                    )
                kind = PhysicalNodeKind.ALIAS
                raw = event.anchor or ""
                metadata.append(
                    PhysicalMetadataEntry(key="alias_target", value=anchors[raw])
                )
            else:
                if event.tag is not None:
                    if event.tag not in _SAFE_TAGS:
                        raise rejected("yaml_unsafe_tag")
                    metadata.append(PhysicalMetadataEntry(key="tag", value=event.tag))
                if event.anchor is not None:
                    budget.check("name_chars", len(event.anchor), limits.max_name_chars)
                    budget.check("anchors", len(anchors) + 1, limits.max_anchors)
                    if event.anchor in anchors:
                        raise malformed(
                            "invalid_yaml_anchor",
                            line_number=line_offset + event.start_mark.line + 1,
                        )
                    anchors[event.anchor] = f"node-{budget.nodes}"
                    metadata.append(
                        PhysicalMetadataEntry(key="anchor", value=event.anchor)
                    )
            if isinstance(event, MappingStartEvent):
                kind = PhysicalNodeKind.MAPPING
            elif isinstance(event, SequenceStartEvent):
                kind = PhysicalNodeKind.SEQUENCE
            if isinstance(event, CollectionStartEvent):
                metadata.append(
                    PhysicalMetadataEntry(key="flow_style", value=event.flow_style)
                )
            if isinstance(event, ScalarEvent):
                metadata.append(PhysicalMetadataEntry(key="style", value=event.style))
            lex_size = event.end_mark.index - event.start_mark.index
            budget.check("value_chars", lex_size, limits.max_value_chars)
            if kind in {PhysicalNodeKind.SCALAR, PhysicalNodeKind.ALIAS}:
                budget.text(len(raw))
                chars += len(raw)
                budget.check("subtree_chars", chars, limits.max_subtree_chars)
                value = ExtractedValue(
                    value_id=f"value-{budget.nodes}",
                    raw_value=StringScalar(value=raw),
                    location=location,
                )
            node = ExtractedTreeNode(
                node_id=f"node-{budget.nodes}",
                parent_id=trees[parent.index].node_id if parent else None,
                name=kind.value,
                node_kind=kind,
                order=order,
                location=location,
                value=value,
                raw_lexeme=text[event.start_mark.index : event.end_mark.index],
                metadata=tuple(metadata),
            )
            if parent:
                parent.child_count += 1
            trees.append(node)
            if isinstance(event, CollectionStartEvent):
                stack.append(_Frame(len(trees) - 1, path))
    except YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = getattr(mark, "line", 0)
        if not allow_incomplete:
            raise malformed(
                "invalid_yaml",
                line_number=line_offset + (line if type(line) is int else 0) + 1,
            ) from None
    finally:
        loader.dispose()
    return MarkupUnit(tuple(trees))


async def _lines(
    source: SourceArtifact,
    context: ParseContext,
    budget: Budget,
    limits: YamlParserLimits,
) -> AsyncGenerator[str]:
    pending = ""
    async for chunk in utf8_chunks(source, context, budget):
        pending += chunk
        while True:
            ends = [i for i in (pending.find("\r"), pending.find("\n")) if i >= 0]
            if not ends:
                break
            index = min(ends)
            if pending[index] == "\r" and index + 1 == len(pending):
                break
            end = index + (2 if pending[index : index + 2] == "\r\n" else 1)
            budget.check("yaml_line_chars", end, limits.max_document_chars)
            yield pending[:end]
            pending = pending[end:]
        budget.check("yaml_line_chars", len(pending), limits.max_document_chars)
    if pending:
        yield pending


async def _documents(
    source: SourceArtifact,
    context: ParseContext,
    limits: YamlParserLimits,
    budget: Budget,
) -> AsyncGenerator[MarkupUnit]:
    parts: list[str] = []
    chars = line_offset = line_number = document_index = 0
    has_document = False
    ended = False
    async for line in _lines(source, context, budget, limits):
        marker = line.startswith("---") and (len(line) == 3 or line[3] in " \t\r\n")
        if has_document and (marker or (ended and line.startswith("%"))):
            budget.check("documents", document_index + 1, limits.max_documents)
            yield await _document(
                "".join(parts),
                source=source,
                budget=budget,
                limits=limits,
                document_index=document_index,
                line_offset=line_offset,
            )
            document_index += 1
            parts.clear()
            chars = 0
            line_offset = line_number
            has_document = False
            ended = False
        budget.check("document_chars", chars + len(line), limits.max_document_chars)
        parts.append(line)
        chars += len(line)
        line_number += 1
        has_document |= marker or (
            bool(line.strip()) and not line.startswith(("#", "%"))
        )
        ended |= line.startswith("...") and (len(line) == 3 or line[3] in " \t\r\n")
    if chars:
        budget.check("documents", document_index + 1, limits.max_documents)
        yield await _document(
            "".join(parts),
            source=source,
            budget=budget,
            limits=limits,
            document_index=document_index,
            line_offset=line_offset,
        )


def _is_json_family(text: str) -> bool:
    """Полный JSON/NDJSON sample принадлежит JSON adapters, не YAML superset."""

    try:
        json.loads(text, parse_int=str, parse_float=str)
    except ValueError:
        records = 0
        try:
            for line in text.split("\n"):
                if line.strip(" \t\r"):
                    json.loads(line, parse_int=str, parse_float=str)
                    records += 1
        except ValueError:
            return False
        except RecursionError:
            return True
        return records > 1
    except RecursionError:
        return True
    return True


class YamlParser:
    """Извлечь иерархию YAML через SafeLoader events без Python constructors.

    Args:
        limits: Неизменяемые YamlParserLimits; ``None`` выбирает defaults.

    Требуется extra ``yaml`` и strict UTF-8. Scalar lexemes, duplicate/complex
    keys и marks сохраняются; aliases остаются ограниченными ссылками, без
    expansion. Comments и точные byte offsets не представлены.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Отсутствует extra (``PARSER_DEPENDENCY_UNAVAILABLE``),
            некорректны YAML или UTF-8 (``PARSER_MALFORMED_INPUT`` /
            ``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: Unsafe tags (``SECURITY_INPUT_REJECTED``) либо
            превышение бюджета (``SECURITY_LIMIT_EXCEEDED``).
    """

    adapter_id = "builtin.yaml"
    version = "1.0.0"
    priority = 35

    def __init__(self, *, limits: YamlParserLimits | None = None) -> None:
        self._limits = limits if limits is not None else YamlParserLimits()
        if type(self._limits) is not YamlParserLimits:
            raise ValueError("limits должен быть YamlParserLimits")

    @property
    def limits(self) -> YamlParserLimits:
        """Вернуть неизменяемые ограничения данного экземпляра."""

        return self._limits

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по bounded sample из reader для данного source.

        JSON/NDJSON уступаются JSON adapters. Probe не создаёт Python objects
        из YAML tags и не подтверждает непрочитанную часть snapshot.
        """

        sample = await probe_bytes(source, context, self.limits)
        try:
            text = codecs.getincrementaldecoder("utf-8-sig")("strict").decode(
                sample, final=len(sample) == source.size_bytes
            )
        except UnicodeDecodeError:
            text = ""
        head = next(
            (
                line.lstrip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            "",
        )
        # Структура начинается в первой значимой строке. Двоеточие в последующей
        # CSV cell не даёт YAML права отклонять весь источник во время selection.
        candidate = (
            bool(head)
            and not head.startswith(("<", '"'))
            and (
                head.startswith(
                    ("---", "%YAML", "%TAG", "- ", "!!", "{", "[", "? ", "&")
                )
                or ": " in head
                or head.endswith(":")
            )
        )
        supported = False
        if (len(sample) < source.size_bytes and head.startswith(("{", "["))) or (
            len(sample) == source.size_bytes and _is_json_family(text)
        ):
            candidate = False
        if candidate:
            parse = ParseContext(
                reader=context.reader,
                source_fingerprint=context.source_fingerprint,
                max_bytes=max(1, source.size_bytes),
                max_records=100000,
                max_nesting_depth=self.limits.max_depth,
            )
            budget = Budget(self.adapter_id, self.limits, parse)
            first_parts: list[str] = []
            has_document = False
            ended = False
            for line in text.splitlines(keepends=True):
                marker = line.startswith("---") and (
                    len(line) == 3 or line[3] in " \t\r\n"
                )
                if has_document and (marker or (ended and line.startswith("%"))):
                    break
                first_parts.append(line)
                has_document |= marker or (
                    bool(line.strip()) and not line.startswith(("#", "%"))
                )
                ended |= line.startswith("...") and (
                    len(line) == 3 or line[3] in " \t\r\n"
                )
            unit = await _document(
                "".join(first_parts),
                source=source,
                budget=budget,
                limits=self.limits,
                document_index=0,
                line_offset=0,
                allow_incomplete=len(sample) < source.size_bytes,
            )
            supported = bool(unit.trees)
        return probe_result(
            source,
            adapter_id=self.adapter_id,
            supported=supported,
            format_id="yaml",
            media_types=frozenset(
                {"application/yaml", "text/yaml", "application/x-yaml"}
            ),
            extensions=frozenset({".yaml", ".yml"}),
            encoding="utf-8",
            warnings=(
                "YAML_SCALARS_NOT_COERCED",
                "YAML_ALIASES_NOT_EXPANDED",
                "ENCODING_UTF8_REQUIRED",
            ),
        )

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch для source и reader/лимитов context.

        Batch boundaries проходят между целыми YAML documents. Чтение, typed
        errors и cancellation происходят при итерации; семантической схемы нет.
        """

        budget = Budget(self.adapter_id, self.limits, context)
        return markup_batches(
            source,
            context,
            budget,
            MarkupDocument(),
            _documents(source, context, self.limits, budget),
        )


__all__ = ("YamlParser", "YamlParserLimits")
