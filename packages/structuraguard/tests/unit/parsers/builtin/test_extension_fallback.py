"""Расширение разрешает неоднозначность, не подменяя проверку содержимого."""

from __future__ import annotations

import pytest
from tests.unit.parsers.builtin._log_fixtures import SYSLOG_JSON
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.exceptions import ParserError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    builtin_delimited_parsers,
    builtin_json_parsers,
    builtin_markup_parsers,
    builtin_text_parsers,
)

_PARTIAL_LOG = b'"context": ""}\n' + SYSLOG_JSON
_MARKDOWN_YAML = b"# Contacts\n\nName: Alpha\n\nName: Beta\n"


def _registry() -> ParserRegistry:
    registry = ParserRegistry()
    for group in (
        builtin_text_parsers,
        builtin_delimited_parsers,
        builtin_json_parsers,
        builtin_markup_parsers,
    ):
        registry.register_many(group())
    return registry


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "name", "expected"),
    (
        (_PARTIAL_LOG, "events.log", "log"),
        (_PARTIAL_LOG, "events.LOG", "log"),
        (_PARTIAL_LOG, "events.txt", "txt"),
        (_MARKDOWN_YAML, "notes.md", "markdown"),
        (_MARKDOWN_YAML, "notes.yaml", "yaml"),
        (_MARKDOWN_YAML, "notes.txt", "txt"),
        (b"ordinary prose\n", "notes.md", "markdown"),
        (b"unrecognized event\n", "events.log", "log"),
    ),
)
async def test_extension_resolves_compatible_content_candidates(
    content: bytes,
    name: str,
    expected: str,
) -> None:
    source = source_for(content, display_name=name)
    probe, parse = contexts_for(source, content)
    async with _registry().session() as session:
        selected = await session.select(source, probe)
        assert selected.probe_result.format_id == expected
        assert "PARSER_EXTENSION_FALLBACK" in selected.probe_result.warnings
        batches = [batch async for batch in selected.parse(source, parse)]
    if expected in {"log", "markdown", "txt"}:
        lines = [line for batch in batches for line in batch.lines]
        assert [line.text for line in lines] == content.decode().splitlines()
        assert [line.line_number for line in lines] == list(range(1, len(lines) + 1))


@pytest.mark.anyio
@pytest.mark.parametrize("name", ("unknown", "unknown.bin", "unknown.csv"))
async def test_conflict_without_compatible_extension_remains_explicit(
    name: str,
) -> None:
    source = source_for(_MARKDOWN_YAML, display_name=name)
    probe, _ = contexts_for(source, _MARKDOWN_YAML)
    async with _registry().session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe)
    assert raised.value.error_code == "PARSER_FORMAT_CONFLICT"


@pytest.mark.anyio
@pytest.mark.parametrize("name", ("claimed.log", "claimed.md", "claimed.txt"))
async def test_unique_content_format_wins_over_extension(name: str) -> None:
    content = b'{"first":1}\n{"second":2}\n'
    source = source_for(content, display_name=name)
    probe, _ = contexts_for(source, content)
    async with _registry().session() as session:
        selected = await session.select(source, probe)
    assert selected.probe_result.format_id == "jsonl"
    assert "PARSER_EXTENSION_FALLBACK" not in selected.probe_result.warnings


@pytest.mark.anyio
@pytest.mark.parametrize("name", ("spoof.log", "spoof.md", "spoof.txt"))
async def test_extension_does_not_accept_binary_content(name: str) -> None:
    content = b"\x00\x01\x02\x00"
    source = source_for(content, display_name=name)
    probe, _ = contexts_for(source, content)
    async with _registry().session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe)
    assert raised.value.error_code == "PARSER_UNSUPPORTED_FORMAT"
