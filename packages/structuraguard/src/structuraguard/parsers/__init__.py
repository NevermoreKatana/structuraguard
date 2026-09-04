"""Публичная композиция и выбор technical parsers."""

from structuraguard.parsers._registration import ParserIdentity
from structuraguard.parsers.discovery import (
    PARSER_ENTRY_POINT_GROUP,
    ParserPluginDiscoveryFailure,
    ParserPluginDiscoveryReport,
    discover_parser_plugins,
)
from structuraguard.parsers.execution import SelectedParser, ValidatedParserStream
from structuraguard.parsers.registry import (
    ParserRegistry,
    ParserRegistrySession,
    ParserRegistrySnapshot,
)

__all__ = (
    "PARSER_ENTRY_POINT_GROUP",
    "ParserIdentity",
    "ParserPluginDiscoveryFailure",
    "ParserPluginDiscoveryReport",
    "ParserRegistry",
    "ParserRegistrySession",
    "ParserRegistrySnapshot",
    "SelectedParser",
    "ValidatedParserStream",
    "discover_parser_plugins",
)
