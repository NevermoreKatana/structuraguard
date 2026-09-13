"""Компиляция resource maximum в существующие builtin limits без второго parser."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, replace
from typing import TYPE_CHECKING, cast

from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    DocxParser,
    HtmlParser,
    JsonDocumentParser,
    JsonLinesParser,
    LogParser,
    MarkdownParser,
    PdfParser,
    PlainTextParser,
    XlsxParser,
    XmlParser,
    YamlParser,
)
from structuraguard.parsers.builtin._common import TextParserLimits
from structuraguard.parsers.builtin._json import JsonParserLimits
from structuraguard.parsers.builtin._markup import MarkupLimits
from structuraguard.ports.parser import Parser
from structuraguard.ports.source import ParseContext

if TYPE_CHECKING:
    from .session import SecuritySession


def bounded_parser(
    session: SecuritySession, parser: Parser, *, context: ParseContext | None = None
) -> Parser:
    policy = session.policy
    if policy.parser_trust != "trusted":
        session.deny("SECURITY_SANDBOX_REQUIRED")
    supported = {
        PlainTextParser: ("txt",),
        LogParser: ("log",),
        MarkdownParser: ("markdown",),
        DelimitedTextParser: ("csv", "tsv"),
        JsonDocumentParser: ("json",),
        JsonLinesParser: ("jsonl",),
        XmlParser: ("xml",),
        HtmlParser: ("html",),
        YamlParser: ("yaml",),
        XlsxParser: ("xlsx",),
        PdfParser: ("pdf",),
        DocxParser: ("docx",),
    }
    if type(parser) not in supported or not set(supported[type(parser)]) & set(
        policy.allowed_formats
    ):
        session.deny("SECURITY_INPUT_REJECTED")
    if policy.strict_mode and set(supported[type(parser)]) & set(policy.risky_formats):
        session.deny("SECURITY_SANDBOX_REQUIRED")
    if not isinstance(
        parser,
        (
            PlainTextParser,
            LogParser,
            MarkdownParser,
            DelimitedTextParser,
            JsonDocumentParser,
            JsonLinesParser,
            XmlParser,
            HtmlParser,
            YamlParser,
            XlsxParser,
            PdfParser,
            DocxParser,
        ),
    ):
        session.deny("SECURITY_INPUT_REJECTED")
    limits = policy.limits
    text = (
        min(limits.max_text_chars, context.max_text_chars)
        if context
        else limits.max_text_chars
    )
    columns = (
        min(limits.max_columns, context.max_columns) if context else limits.max_columns
    )
    caps = {
        "read_chunk_bytes": limits.read_chunk_bytes,
        "max_columns": columns,
        "max_attributes_per_node": columns,
        "max_line_chars": text,
        "max_record_chars": text,
        "max_value_chars": text,
        "max_field_size": text,
        "max_text_chars": text,
        "max_batch_chars": text,
        "max_block_chars": text,
        "max_subtree_chars": text,
        "max_depth": limits.max_nesting_depth,
        "max_nesting_depth": limits.max_nesting_depth,
    }

    def narrow[L: TextParserLimits | JsonParserLimits | MarkupLimits](local: L) -> L:
        changes: dict[str, int] = {}
        for field in fields(local):
            if field.name in caps:
                current = getattr(local, field.name)
                if type(current) is not int or current <= 0:
                    session.deny("SECURITY_POLICY_INVALID")
                changes[field.name] = min(current, caps[field.name])
        # Список kwargs ограничен реальными numeric dataclass fields выше;
        # dataclasses.replace сохраняет точный тип local, включая subclass.
        clone = cast(Callable[..., L], replace)
        return clone(local, **changes)

    try:
        if isinstance(parser, PlainTextParser):
            return PlainTextParser(limits=narrow(parser.limits))
        if isinstance(parser, LogParser):
            return LogParser(limits=narrow(parser.limits))
        if isinstance(parser, MarkdownParser):
            return MarkdownParser(limits=narrow(parser.limits))
        if isinstance(parser, DelimitedTextParser):
            return DelimitedTextParser(
                limits=narrow(parser.limits), detection_options=parser.detection_options
            )
        if isinstance(parser, JsonDocumentParser):
            return JsonDocumentParser(limits=narrow(parser.limits))
        if isinstance(parser, JsonLinesParser):
            return JsonLinesParser(limits=narrow(parser.limits))
        if isinstance(parser, XmlParser):
            return XmlParser(limits=narrow(parser.limits))
        if isinstance(parser, HtmlParser):
            return HtmlParser(limits=narrow(parser.limits))
        if isinstance(parser, YamlParser):
            return YamlParser(limits=narrow(parser.limits))
        if isinstance(parser, XlsxParser):
            return XlsxParser(limits=narrow(parser.limits))
        if isinstance(parser, PdfParser):
            return PdfParser(limits=narrow(parser.limits))
        if isinstance(parser, DocxParser):
            return DocxParser(limits=narrow(parser.limits))
    except (TypeError, ValueError):
        session.deny("SECURITY_POLICY_INVALID")
    session.deny("SECURITY_INPUT_REJECTED")
