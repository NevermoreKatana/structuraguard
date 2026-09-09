"""Явная композиция независимых встроенных technical parsers."""

from structuraguard.parsers.builtin._common import (
    LogParserLimits,
    MarkdownParserLimits,
    TextParserLimits,
)
from structuraguard.parsers.builtin._json import JsonParserLimits
from structuraguard.parsers.builtin.delimited import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedParserLimits,
    DelimitedTextParser,
)
from structuraguard.parsers.builtin.docx import DocxParser, DocxParserLimits
from structuraguard.parsers.builtin.html import (
    HtmlParser,
    HtmlParserLimits,
    html_safe_json,
)
from structuraguard.parsers.builtin.json_document import JsonDocumentParser
from structuraguard.parsers.builtin.json_lines import JsonLinesParser
from structuraguard.parsers.builtin.log import LogParser
from structuraguard.parsers.builtin.markdown import MarkdownParser
from structuraguard.parsers.builtin.pdf import PdfParser, PdfParserLimits
from structuraguard.parsers.builtin.text import PlainTextParser
from structuraguard.parsers.builtin.xlsx import XlsxParser, XlsxParserLimits
from structuraguard.parsers.builtin.xml import XmlParser, XmlParserLimits
from structuraguard.parsers.builtin.yaml import YamlParser, YamlParserLimits


def builtin_text_parsers() -> tuple[PlainTextParser, LogParser, MarkdownParser]:
    """Вернуть новые instance-local adapters для явной регистрации владельцем SDK."""

    return PlainTextParser(), LogParser(), MarkdownParser()


def builtin_delimited_parsers() -> tuple[DelimitedTextParser]:
    """Вернуть новый instance-local CSV/TSV family adapter."""

    return (DelimitedTextParser(),)


def builtin_json_parsers() -> tuple[JsonDocumentParser, JsonLinesParser]:
    """Вернуть новые instance-local JSON document/lines adapters."""

    return JsonDocumentParser(), JsonLinesParser()


def builtin_markup_parsers() -> tuple[XmlParser, HtmlParser, YamlParser]:
    """Новые независимые XML/HTML/YAML adapters без eager загрузки extras."""

    return XmlParser(), HtmlParser(), YamlParser()


def builtin_document_parsers() -> tuple[XlsxParser, PdfParser, DocxParser]:
    """Новые независимые document adapters с lazy optional dependencies."""

    return XlsxParser(), PdfParser(), DocxParser()


__all__ = (
    "DelimitedDetectionOptions",
    "DelimitedDialect",
    "DelimitedParserLimits",
    "DelimitedTextParser",
    "DocxParser",
    "DocxParserLimits",
    "HtmlParser",
    "HtmlParserLimits",
    "JsonDocumentParser",
    "JsonLinesParser",
    "JsonParserLimits",
    "LogParser",
    "LogParserLimits",
    "MarkdownParser",
    "MarkdownParserLimits",
    "PdfParser",
    "PdfParserLimits",
    "PlainTextParser",
    "TextParserLimits",
    "XlsxParser",
    "XlsxParserLimits",
    "XmlParser",
    "XmlParserLimits",
    "YamlParser",
    "YamlParserLimits",
    "builtin_delimited_parsers",
    "builtin_document_parsers",
    "builtin_json_parsers",
    "builtin_markup_parsers",
    "builtin_text_parsers",
    "html_safe_json",
)
