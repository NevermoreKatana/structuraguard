"""Инертный HTML source-event DOM; не browser и не HTML5 rendering engine."""

from __future__ import annotations

import codecs
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

from structuraguard.contracts.common import NullScalar, StringScalar
from structuraguard.contracts.source import (
    CssSelectorLocation,
    ExtractedBatch,
    ExtractedBlock,
    ExtractedBlockKind,
    ExtractedCell,
    ExtractedTable,
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
    probe_bytes,
    probe_result,
    utf8_chunks,
)

_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_DENIED = frozenset(
    {"script", "style", "iframe", "object", "embed", "template", "svg", "math"}
)
_HTML_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "title",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "span",
        "table",
        "ul",
        "ol",
        "li",
        "a",
        "form",
        "img",
        "script",
        "style",
        "iframe",
        "meta",
        "link",
        "section",
        "article",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class HtmlParserLimits(MarkupLimits):
    """Пределы HTML, включая attributes до physical projection."""

    max_attributes_per_node: int = 256

    def __post_init__(self) -> None:
        MarkupLimits.__post_init__(self)
        if (
            type(self.max_attributes_per_node) is not int
            or not 1 <= self.max_attributes_per_node <= 100000
        ):
            raise ValueError("max_attributes_per_node должен быть int от 1 до 100000")


@dataclass(slots=True)
class _Frame:
    node: ExtractedTreeNode
    selector: str
    denied: bool
    order: int = 0
    elements: int = 0
    text: list[str] = field(default_factory=list)
    lexical: list[str] = field(default_factory=list)
    chars: int = 0
    lex_chars: int = 0
    position: tuple[int, int] = (1, 0)


class _Dom(HTMLParser):
    def __init__(
        self,
        source: SourceArtifact,
        budget: Budget,
        limits: HtmlParserLimits,
        document: MarkupDocument,
    ) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.budget = budget
        self.limits = limits
        budget.node(depth=0)
        root = ExtractedTreeNode(
            node_id="node-1",
            name="document",
            node_kind=PhysicalNodeKind.DOCUMENT,
            order=0,
            location=CssSelectorLocation(source=source.ref, selector=":scope"),
        )
        document.root = root
        self.stack = [_Frame(root, ":scope", False)]
        self.pending: list[ExtractedTreeNode] = []
        self.pending_chars = 0
        self.ready: list[MarkupUnit] = []
        self.recognized = False
        self.xhtml = False
        self._buffer_start = self.getpos()

    def feed(self, data: str) -> None:
        self._buffer_start = self.getpos()
        self.budget.check(
            "html_buffer_chars",
            len(self.rawdata) + len(data),
            self.limits.max_token_chars + self.limits.read_chunk_bytes,
        )
        try:
            super().feed(data)
        except AssertionError:
            raise malformed("invalid_html", line_number=self.getpos()[0]) from None
        self.budget.check("token_chars", len(self.rawdata), self.limits.max_token_chars)

    def parse_html_declaration(self, i: int) -> int:
        # Новые CPython направляют неизвестные <![... в bogus comments.
        # Сохраняем прежнюю marked-section grammar и typed отказ через feed/finish,
        # не затрагивая такой же текст внутри attributes, comments и raw text.
        if self.rawdata.startswith("<![", i):
            return self.parse_marked_section(i)
        return super().parse_html_declaration(i)

    def location(
        self, selector: str, order: int, position: tuple[int, int]
    ) -> CssSelectorLocation:
        self.budget.check("selector_chars", len(selector), 4096)
        return CssSelectorLocation(
            source=self.source.ref,
            selector=selector,
            node_index=order,
            line_number=position[0],
            column_number=position[1],
        )

    def add(self, node: ExtractedTreeNode) -> None:
        self.budget.check(
            "subtree_nodes", len(self.pending) + 1, self.limits.max_subtree_nodes
        )
        size = (
            len(node.value.raw_value.value)
            if node.value and isinstance(node.value.raw_value.value, str)
            else 0
        )
        self.budget.check(
            "subtree_chars", self.pending_chars + size, self.limits.max_subtree_chars
        )
        self.pending_chars += size
        self.pending.append(node)

    def flush_unit(self) -> None:
        if self.pending:
            trees = tuple(self.pending)
            blocks, tables = _project(trees, self.budget)
            self.ready.append(MarkupUnit(trees, blocks, tables))
            self.pending.clear()
            self.pending_chars = 0

    def leaf(
        self,
        kind: PhysicalNodeKind,
        name: str,
        value: str | None,
        position: tuple[int, int],
        *,
        lexeme: str | None = None,
    ) -> None:
        frame = self.stack[-1]
        self.budget.node(depth=len(self.stack) - 1, name=name)
        location = self.location(frame.selector, frame.order, position)
        node = ExtractedTreeNode(
            node_id=f"node-{self.budget.nodes}",
            parent_id=frame.node.node_id,
            name=kind.value,
            raw_name=name,
            node_kind=kind,
            order=frame.order,
            location=location,
            raw_lexeme=lexeme,
            value=ExtractedValue(
                value_id=f"value-{self.budget.nodes}",
                location=location,
                raw_value=NullScalar() if value is None else StringScalar(value=value),
            ),
            metadata=(
                PhysicalMetadataEntry(key="active_content_policy", value="denied"),
            )
            if frame.denied
            else (),
        )
        frame.order += 1
        self.add(node)
        if len(self.stack) == 1:
            self.flush_unit()

    def flush_text(self) -> None:
        frame = self.stack[-1]
        if frame.lexical:
            self.leaf(
                PhysicalNodeKind.TEXT,
                "#text",
                "".join(frame.text),
                frame.position,
                lexeme="".join(frame.lexical),
            )
            frame.text.clear()
            frame.lexical.clear()
            frame.chars = frame.lex_chars = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.flush_text()
        self.budget.check(
            "token_chars",
            len(self.get_starttag_text() or ""),
            self.limits.max_token_chars,
        )
        self.recognized |= tag in _HTML_TAGS
        self.xhtml |= (
            tag == "html" and ("xmlns", "http://www.w3.org/1999/xhtml") in attrs
        )
        self.budget.check("attributes", len(attrs), self.limits.max_attributes_per_node)
        self.budget.node(depth=len(self.stack), name=tag)
        parent = self.stack[-1]
        parent.elements += 1
        selector = f"{parent.selector} > *:nth-child({parent.elements})"
        denied = parent.denied or tag in _DENIED
        node = ExtractedTreeNode(
            node_id=f"node-{self.budget.nodes}",
            parent_id=parent.node.node_id,
            name="element",
            raw_name=tag,
            node_kind=PhysicalNodeKind.ELEMENT,
            order=parent.order,
            location=self.location(selector, parent.order, self.getpos()),
            raw_lexeme=self.get_starttag_text(),
            metadata=(
                PhysicalMetadataEntry(key="active_content_policy", value="denied"),
            )
            if denied
            else (),
        )
        parent.order += 1
        self.add(node)
        self.stack.append(_Frame(node, selector, denied))
        for name, value in attrs:
            self.budget.text(len(value or ""))
            self.leaf(PhysicalNodeKind.ATTRIBUTE, name, value, self.getpos())
        if tag in _VOID:
            self.handle_endtag(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self.flush_text()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].node.raw_name == tag:
                del self.stack[index:]
                if len(self.stack) == 1:
                    self.flush_unit()
                return

    def _text(self, value: str, lexeme: str) -> None:
        frame = self.stack[-1]
        self.budget.text(len(value), existing=frame.chars)
        self.budget.check(
            "value_chars", frame.lex_chars + len(lexeme), self.limits.max_value_chars
        )
        self.budget.check(
            "subtree_chars",
            self.pending_chars + frame.chars + len(value),
            self.limits.max_subtree_chars,
        )
        if not frame.lexical:
            frame.position = self.getpos()
        frame.text.append(value)
        frame.lexical.append(lexeme)
        frame.chars += len(value)
        frame.lex_chars += len(lexeme)

    def handle_data(self, data: str) -> None:
        self._text(data, data)

    def handle_entityref(self, name: str) -> None:
        self._reference(f"&{name}")

    def handle_charref(self, name: str) -> None:
        self._reference(f"&#{name}")

    def _reference(self, prefix: str) -> None:
        line, column = self.getpos()
        base_line, base_column = self._buffer_start
        offset = 0
        for _ in range(line - base_line):
            offset = self.rawdata.index("\n", offset) + 1
        offset += column - (base_column if line == base_line else 0)
        end = offset + len(prefix)
        lexeme = prefix + (";" if self.rawdata[end : end + 1] == ";" else "")
        self._text(unescape(lexeme), lexeme)

    def handle_comment(self, data: str) -> None:
        self.flush_text()
        self.budget.text(len(data))
        self.leaf(PhysicalNodeKind.COMMENT, "#comment", data, self.getpos())

    def handle_decl(self, decl: str) -> None:
        self.recognized |= decl.lower().split(maxsplit=2)[:2] == ["doctype", "html"]
        self.flush_text()
        self.budget.text(len(decl))
        self.leaf(PhysicalNodeKind.DECLARATION, "#declaration", decl, self.getpos())

    def unknown_decl(self, data: str) -> None:
        self.handle_decl(data)

    def handle_pi(self, data: str) -> None:
        self.flush_text()
        self.budget.text(len(data))
        self.leaf(PhysicalNodeKind.PROCESSING_INSTRUCTION, "#pi", data, self.getpos())

    def finish(self) -> None:
        self._buffer_start = self.getpos()
        try:
            self.close()
        except AssertionError:
            raise malformed("invalid_html", line_number=self.getpos()[0]) from None
        # Незакрытый script сохраняется как denied text, не теряется в HTMLParser.rawdata.
        if self.rawdata:
            self.handle_data(self.rawdata)
            self.rawdata = ""
        self.flush_text()
        del self.stack[1:]
        self.flush_unit()


def _project(
    trees: tuple[ExtractedTreeNode, ...], budget: Budget
) -> tuple[tuple[ExtractedBlock, ...], tuple[ExtractedTable, ...]]:
    children: dict[str, list[ExtractedTreeNode]] = {}
    for node in trees:
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node)

    def text_content(root: ExtractedTreeNode) -> str:
        parts: list[str] = []
        size = 0
        stack = [root]
        while stack:
            node = stack.pop()
            if node.metadata:
                continue
            if node.node_kind is PhysicalNodeKind.TEXT and node.value is not None:
                raw = node.value.raw_value.value
                if isinstance(raw, str):
                    size += len(raw)
                    budget.check("block_chars", size, budget.limits.max_subtree_chars)
                    parts.append(raw)
            stack.extend(reversed(children.get(node.node_id, [])))
        return "".join(parts)

    blocks: list[ExtractedBlock] = []
    tables: list[ExtractedTable] = []
    projection_chars = 0
    for node in trees:
        if node.node_kind is not PhysicalNodeKind.ELEMENT or node.metadata:
            continue
        tag = node.raw_name
        kind = None
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            kind = ExtractedBlockKind.HEADING
        elif tag in {"p", "div", "pre", "blockquote", "li"}:
            kind = ExtractedBlockKind.PARAGRAPH
        elif tag in {"ul", "ol", "dl"}:
            kind = ExtractedBlockKind.LIST
        if kind is not None:
            text = text_content(node)
            projection_chars += len(text)
            budget.check(
                "projection_chars", projection_chars, budget.limits.max_subtree_chars
            )
            blocks.append(
                ExtractedBlock(
                    block_id=node.node_id.replace("node-", "block-"),
                    kind=kind,
                    order=node.order,
                    text=text,
                    location=node.location,
                )
            )
        if tag != "table":
            continue
        rows: list[ExtractedTreeNode] = []
        stack = list(reversed(children.get(node.node_id, [])))
        while stack:
            child = stack.pop()
            if child.raw_name == "table" or child.metadata:
                continue
            if child.raw_name == "tr":
                rows.append(child)
            else:
                stack.extend(reversed(children.get(child.node_id, [])))
        cells: list[ExtractedCell] = []
        for row_index, row in enumerate(rows):
            row_cells = [
                c for c in children.get(row.node_id, []) if c.raw_name in {"td", "th"}
            ]
            for column, cell in enumerate(row_cells):
                text = text_content(cell)
                projection_chars += len(text)
                budget.check(
                    "projection_chars",
                    projection_chars,
                    budget.limits.max_subtree_chars,
                )
                cells.append(
                    ExtractedCell(
                        cell_id=cell.node_id.replace("node-", "cell-"),
                        row_index=row_index,
                        column_index=column,
                        value=ExtractedValue(
                            value_id=f"value-{2_000_000_000 + int(cell.node_id.removeprefix('node-'))}",
                            raw_value=StringScalar(value=text),
                            location=cell.location,
                        ),
                    )
                )
        tables.append(
            ExtractedTable(
                table_id=node.node_id.replace("node-", "table-"),
                location=node.location,
                cells=tuple(cells),
            )
        )
    return tuple(blocks), tuple(tables)


async def _units(
    source: SourceArtifact,
    context: ParseContext,
    limits: HtmlParserLimits,
    budget: Budget,
    document: MarkupDocument,
) -> AsyncGenerator[MarkupUnit]:
    parser = _Dom(source, budget, limits, document)
    async for chunk in utf8_chunks(source, context, budget):
        parser.feed(chunk)
        for unit in parser.ready:
            yield unit
        parser.ready.clear()
    parser.finish()
    for unit in parser.ready:
        yield unit


def html_safe_json(batch: ExtractedBatch) -> str:
    """Сериализовать batch в JSON с экранированием HTML delimiters и U+2028/2029.

    Args:
        batch: Физический результат с потенциально вредоносными raw values.

    Returns:
        JSON-строка для data embedding, без изменения исходного batch.

    Это не HTML sanitizer: после JSON decoding raw поля по-прежнему недоверенные
    и не предназначены для ``innerHTML``, HTML attributes или выполнения кода.
    """

    return (
        batch.canonical_json()
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


class HtmlParser:
    """Извлечь физический source DOM без браузера, JavaScript и загрузки ресурсов.

    Args:
        limits: Неизменяемые HtmlParserLimits; ``None`` выбирает defaults.

    Только strict UTF-8. Headings, lists, blocks и tables связаны с деревом
    source events, а не исправленным browser DOM. Script/style/iframe остаются
    неактивными данными; raw output требует escaping при отображении.
    Завершённая marked section с неизвестным именем отклоняется как malformed input;
    CDATA и поддержанные conditional sections сохраняются как physical declarations.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Некорректны данные (``PARSER_MALFORMED_INPUT``) или UTF-8
            (``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: Превышены limits (``SECURITY_LIMIT_EXCEEDED``)
            либо запрещена конструкция (``SECURITY_INPUT_REJECTED``).
    """

    adapter_id = "builtin.html"
    version = "1.0.0"
    priority = 46

    def __init__(self, *, limits: HtmlParserLimits | None = None) -> None:
        self._limits = limits if limits is not None else HtmlParserLimits()
        if type(self._limits) is not HtmlParserLimits:
            raise ValueError("limits должен быть HtmlParserLimits")

    @property
    def limits(self) -> HtmlParserLimits:
        """Вернуть неизменяемые ограничения данного экземпляра."""

        return self._limits

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Вернуть ProbeResult по bounded UTF-8 sample из reader/context source.

        XML/XHTML не перехватываются. Probe не загружает external resources и
        не гарантирует корректность непрочитанной части документа.
        """

        sample = await probe_bytes(source, context, self.limits)
        try:
            text = codecs.getincrementaldecoder("utf-8-sig")("strict").decode(
                sample, final=len(sample) == source.size_bytes
            )
        except UnicodeDecodeError:
            text = ""
        parse = ParseContext(
            reader=context.reader,
            source_fingerprint=context.source_fingerprint,
            max_bytes=max(1, source.size_bytes),
            max_records=100000,
            max_nesting_depth=self.limits.max_depth,
        )
        parser = _Dom(
            source,
            Budget(self.adapter_id, self.limits, parse),
            self.limits,
            MarkupDocument(),
        )
        parser.budget.check("probe_token_chars", len(text), self.limits.max_probe_bytes)
        for offset in range(0, len(text), self.limits.read_chunk_bytes):
            parser.feed(text[offset : offset + self.limits.read_chunk_bytes])
            parser.ready.clear()
        return probe_result(
            source,
            adapter_id=self.adapter_id,
            supported=parser.recognized
            and not parser.xhtml
            and not text.lstrip().startswith("<?xml"),
            format_id="html",
            media_types=frozenset({"text/html"}),
            extensions=frozenset({".html", ".htm"}),
            encoding="utf-8",
            warnings=(
                "HTML_SOURCE_DOM_NOT_BROWSER_DOM",
                "HTML_ACTIVE_CONTENT_DENIED",
                "ENCODING_UTF8_REQUIRED",
            ),
        )

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток физических ExtractedBatch для source и limits/context.

        Batches разделяются между top-level subtrees; CSS provenance относится
        к source DOM. Чтение, typed errors и cancellation происходят при итерации.
        """

        budget = Budget(self.adapter_id, self.limits, context)
        document = MarkupDocument()
        return markup_batches(
            source,
            context,
            budget,
            document,
            _units(source, context, self.limits, budget, document),
        )


__all__ = ("HtmlParser", "HtmlParserLimits", "html_safe_json")
