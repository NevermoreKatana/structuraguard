"""Hardened XML events без DTD, external resolver и полного DOM."""

from __future__ import annotations

from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol, cast
from xml.etree.ElementTree import ParseError

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedTreeNode,
    ExtractedValue,
    PhysicalNodeKind,
    ProbeResult,
    SourceArtifact,
    XPathLocation,
)
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import encoding_error
from ._markup import (
    Budget,
    MarkupDocument,
    MarkupLimits,
    MarkupUnit,
    byte_chunks,
    malformed,
    markup_batches,
    missing_extra,
    probe_bytes,
    probe_result,
    rejected,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class XmlParserLimits(MarkupLimits):
    """Пределы XML elements, attributes, namespace declarations и text."""

    max_attributes_per_node: int = 256
    max_namespaces: int = 10000

    def __post_init__(self) -> None:
        MarkupLimits.__post_init__(self)
        for name in ("max_attributes_per_node", "max_namespaces"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 100000:
                raise ValueError(f"{name} должен быть int от 1 до 100000")


def _literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"


def _name_test(name: str) -> str:
    if not name.startswith("{"):
        return name
    namespace, _, local = name[1:].partition("}")
    return (
        f"*[local-name()={_literal(local)} and namespace-uri()={_literal(namespace)}]"
    )


@dataclass(slots=True)
class _Frame:
    node: ExtractedTreeNode
    path: str
    counts: Counter[str] = field(default_factory=Counter)
    order: int = 0
    text: list[str] = field(default_factory=list)
    text_chars: int = 0


class _Target:
    def __init__(
        self,
        source: SourceArtifact,
        budget: Budget,
        limits: XmlParserLimits,
        document: MarkupDocument,
    ) -> None:
        self.source = source
        self.budget = budget
        self.limits = limits
        self.document = document
        self.stack: list[_Frame] = []
        self.pending: list[ExtractedTreeNode] = []
        self.ready: list[MarkupUnit] = []
        self.pending_chars = 0
        self.namespaces: list[tuple[str, str]] = []
        self.namespace_count = 0
        self.top_counts: Counter[str] = Counter()
        self.top_order = 0

    def location(self, path: str) -> XPathLocation:
        self.budget.check("xpath_chars", len(path), 4096)
        return XPathLocation(source=self.source.ref, xpath=path)

    def add(self, node: ExtractedTreeNode) -> None:
        if len(self.stack) <= 1 and node.node_kind is not PhysicalNodeKind.ELEMENT:
            self.ready.append(MarkupUnit((node,)))
            return
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

    def leaf(self, kind: PhysicalNodeKind, name: str, text: str, path: str) -> None:
        self.budget.node(depth=len(self.stack), name=name)
        parent = self.stack[-1] if self.stack else None
        node_id = f"node-{self.budget.nodes}"
        location = self.location(path)
        if kind is PhysicalNodeKind.NAMESPACE:
            location = location.model_copy(update={"namespace_prefix": name})
        node = ExtractedTreeNode(
            node_id=node_id,
            parent_id=parent.node.node_id if parent else None,
            name=kind.value,
            raw_name=name,
            node_kind=kind,
            order=parent.order if parent else self.top_order,
            location=location,
            value=ExtractedValue(
                value_id=f"value-{self.budget.nodes}",
                raw_value=StringScalar(value=text),
                location=location,
            ),
        )
        if parent:
            parent.order += 1
        else:
            self.top_order += 1
        self.add(node)

    def flush_text(self) -> None:
        if not self.stack or not self.stack[-1].text:
            return
        frame = self.stack[-1]
        frame.counts["#text"] += 1
        self.leaf(
            PhysicalNodeKind.TEXT,
            "#text",
            "".join(frame.text),
            f"{frame.path}/text()[{frame.counts['#text']}]",
        )
        frame.text.clear()
        frame.text_chars = 0

    def start_ns(self, prefix: str | None, uri: str | None) -> None:
        self.namespace_count += 1
        self.budget.check(
            "namespaces", self.namespace_count, self.limits.max_namespaces
        )
        self.budget.check("name_chars", len(prefix or ""), self.limits.max_name_chars)
        self.budget.text(len(uri or ""))
        self.namespaces.append((prefix or "", uri or ""))

    def end_ns(self, prefix: str | None) -> None:
        pass

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.flush_text()
        self.budget.node(depth=len(self.stack) + 1, name=tag)
        self.budget.check("attributes", len(attrs), self.limits.max_attributes_per_node)
        parent = self.stack[-1] if self.stack else None
        count = 1
        if parent:
            parent.counts[tag] += 1
            count = parent.counts[tag]
        path = (parent.path if parent else "") + f"/{_name_test(tag)}[{count}]"
        node = ExtractedTreeNode(
            node_id=f"node-{self.budget.nodes}",
            parent_id=parent.node.node_id if parent else None,
            name="element",
            raw_name=tag,
            node_kind=PhysicalNodeKind.ELEMENT,
            order=parent.order if parent else self.top_order,
            location=self.location(path),
        )
        if parent:
            parent.order += 1
            self.add(node)
        else:
            self.document.root = node
            self.top_order += 1
        self.stack.append(_Frame(node, path))
        for prefix, uri in self.namespaces:
            self.leaf(
                PhysicalNodeKind.NAMESPACE,
                prefix,
                uri,
                path,
            )
        self.namespaces.clear()
        for name, value in attrs.items():
            self.budget.text(len(value))
            self.leaf(
                PhysicalNodeKind.ATTRIBUTE, name, value, f"{path}/@{_name_test(name)}"
            )

    def end(self, tag: str) -> None:
        self.flush_text()
        self.stack.pop()
        if len(self.stack) == 1:
            self.ready.append(MarkupUnit(tuple(self.pending)))
            self.pending.clear()
            self.pending_chars = 0

    def data(self, data: str) -> None:
        if not self.stack or not data:
            return
        frame = self.stack[-1]
        self.budget.text(len(data), existing=frame.text_chars)
        self.budget.check(
            "subtree_chars",
            self.pending_chars + frame.text_chars + len(data),
            self.limits.max_subtree_chars,
        )
        frame.text.append(data)
        frame.text_chars += len(data)

    def _misc(self, kind: PhysicalNodeKind, name: str, value: str, test: str) -> None:
        self.flush_text()
        self.budget.text(len(value))
        counts = self.stack[-1].counts if self.stack else self.top_counts
        counts[test] += 1
        path = self.stack[-1].path if self.stack else ""
        self.leaf(kind, name, value, f"{path}/{test}[{counts[test]}]")

    def comment(self, text: str) -> None:
        self._misc(PhysicalNodeKind.COMMENT, "#comment", text, "comment()")

    def pi(self, target: str, text: str) -> None:
        self._misc(
            PhysicalNodeKind.PROCESSING_INSTRUCTION,
            target,
            text,
            f"processing-instruction({_literal(target)})",
        )

    def close(self) -> None:
        return None


class _ExpatPosition(Protocol):
    @property
    def CurrentByteIndex(self) -> int: ...


class _BackendPosition(Protocol):
    @property
    def parser(self) -> _ExpatPosition: ...


def _check_buffer(
    parser: _BackendPosition, budget: Budget, *, fed: int, size: int
) -> None:
    # CurrentByteIndex остаётся на начале незаконченного token. Запас учитывает
    # multibyte encoding и Expat reparse deferral; comment/CDATA не обходят cap.
    budget.check(
        "xml_buffer_bytes",
        fed - parser.parser.CurrentByteIndex + size,
        budget.limits.max_token_chars * 4 + budget.limits.read_chunk_bytes,
    )


async def _units(
    source: SourceArtifact,
    context: ParseContext,
    limits: XmlParserLimits,
    budget: Budget,
    document: MarkupDocument,
) -> AsyncGenerator[MarkupUnit]:
    try:
        from defusedxml.common import DefusedXmlException
        from defusedxml.ElementTree import DefusedXMLParser
    except ImportError:
        raise missing_extra("xml") from None
    target = _Target(source, budget, limits, document)
    parser = DefusedXMLParser(
        target=target, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    position = cast(_BackendPosition, parser)
    fed = 0
    try:
        async for chunk in byte_chunks(source, context, budget):
            _check_buffer(position, budget, fed=fed, size=len(chunk))
            parser.feed(chunk)
            fed += len(chunk)
            for unit in target.ready:
                yield unit
            target.ready.clear()
        parser.close()
        for unit in target.ready:
            yield unit
    except DefusedXmlException:
        raise rejected("xml_dtd_or_entity") from None
    except ParseError as error:
        raise malformed("invalid_xml", line_number=error.position[0]) from None
    except LookupError:
        raise encoding_error(
            reason="unsupported_codec", encoding="xml-declaration"
        ) from None


class XmlParser:
    """Извлечь XML infoset с namespace-aware XPath, без DTD и external entities.

    Args:
        limits: Неизменяемые ограничения узлов, глубины и текста; ``None``
            выбирает ``XmlParserLimits()``. Дополнительно действует ParseContext.

    Требуется extra ``xml``. Сеть не используется. Возвращается дерево символов
    XML infoset, а не точная копия исходной разметки или окончательная схема.

    Raises:
        ValueError: Передан неверный тип limits.
        ParserError: Отсутствует extra (``PARSER_DEPENDENCY_UNAVAILABLE``),
            некорректны XML или кодировка (``PARSER_MALFORMED_INPUT`` /
            ``PARSER_ENCODING_UNSUPPORTED``).
        SecurityPolicyError: DTD/entities (``SECURITY_INPUT_REJECTED``) либо
            превышение бюджета (``SECURITY_LIMIT_EXCEEDED``).
    """

    adapter_id = "builtin.xml"
    version = "1.0.0"
    priority = 45

    def __init__(self, *, limits: XmlParserLimits | None = None) -> None:
        self._limits = limits if limits is not None else XmlParserLimits()
        if type(self._limits) is not XmlParserLimits:
            raise ValueError("limits должен быть XmlParserLimits")

    @property
    def limits(self) -> XmlParserLimits:
        """Вернуть неизменяемые ограничения данного экземпляра."""

        return self._limits

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        """Проверить bounded sample из context.reader и вернуть ProbeResult.

        ``source`` описывает тот же snapshot. XHTML с namespace относится к XML;
        HTML без namespace уступается HTML adapter. Policy errors не подавляются.
        """

        sample = await probe_bytes(source, context, self.limits)
        head = sample.removeprefix(b"\xef\xbb\xbf").lstrip().lower()
        candidate = head.startswith((b"<", b"\xff\xfe", b"\xfe\xff"))
        # HTML doctype/tag soup принадлежит HTML; XHTML с namespace — XML.
        doctype_name = (
            head[len(b"<!doctype") :].lstrip() if head.startswith(b"<!doctype") else b""
        )
        html = (
            doctype_name.startswith(b"html")
            and doctype_name[4:5] in {b">", b" ", b"\t", b"\r", b"\n", b"["}
        ) or (
            head.startswith(b"<html")
            and head[5:6] in {b">", b"/", b" ", b"\t", b"\r", b"\n"}
            and b"xmlns" not in head[:1024]
        )
        supported = False
        if candidate and not html:
            try:
                from defusedxml.common import DefusedXmlException
                from defusedxml.ElementTree import DefusedXMLParser
            except ImportError:
                raise missing_extra("xml") from None
            document = MarkupDocument()
            parse = ParseContext(
                reader=context.reader,
                source_fingerprint=context.source_fingerprint,
                max_bytes=max(1, source.size_bytes),
                max_records=100000,
                max_nesting_depth=self.limits.max_depth,
            )
            target = _Target(
                source,
                Budget(self.adapter_id, self.limits, parse),
                self.limits,
                document,
            )
            parser = DefusedXMLParser(
                target=target,
                forbid_dtd=True,
                forbid_entities=True,
                forbid_external=True,
            )
            try:
                for offset in range(0, len(sample), self.limits.read_chunk_bytes):
                    chunk = sample[offset : offset + self.limits.read_chunk_bytes]
                    _check_buffer(
                        cast(_BackendPosition, parser),
                        target.budget,
                        fed=offset,
                        size=len(chunk),
                    )
                    parser.feed(chunk)
                    target.ready.clear()
                if len(sample) == source.size_bytes:
                    parser.close()
                supported = document.root is not None
            except DefusedXmlException:
                raise rejected("xml_dtd_or_entity") from None
            except (ParseError, LookupError):
                supported = False
        return probe_result(
            source,
            adapter_id=self.adapter_id,
            supported=supported,
            format_id="xml",
            media_types=frozenset(
                {"application/xml", "text/xml", "application/xhtml+xml"}
            ),
            extensions=frozenset({".xml", ".xhtml"}),
        )

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        """Вернуть поток ExtractedBatch для source и reader/лимитов context.

        Batches разделяются между direct-child subtrees; корень имеет continuation
        metadata. Чтение, typed errors и cancellation возникают при итерации.
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


__all__ = ("XmlParser", "XmlParserLimits")
