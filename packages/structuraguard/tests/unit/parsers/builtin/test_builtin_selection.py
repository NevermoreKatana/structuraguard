"""Ownership и explicit registration независимых built-in adapters."""

from __future__ import annotations

import pytest
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    JsonDocumentParser,
    JsonLinesParser,
    LogParser,
    MarkdownParser,
    PlainTextParser,
    builtin_delimited_parsers,
    builtin_json_parsers,
    builtin_text_parsers,
)

_UNKNOWN_KV_LINE = " ".join(f"field{index}=x" for index in range(70)).encode() + b"\n"
_OVERFLOW_KV_LINE = b"level=INFO " + _UNKNOWN_KV_LINE


def test_builtin_text_parsers_returns_three_independent_adapters() -> None:
    first = builtin_text_parsers()
    second = builtin_text_parsers()
    first_by_id = {parser.adapter_id: parser for parser in first}
    second_by_id = {parser.adapter_id: parser for parser in second}

    assert {adapter_id: type(parser) for adapter_id, parser in first_by_id.items()} == {
        "builtin.text": PlainTextParser,
        "builtin.log": LogParser,
        "builtin.markdown": MarkdownParser,
    }
    assert first_by_id.keys() == second_by_id.keys()
    assert all(
        first_by_id[adapter_id] is not second_by_id[adapter_id]
        for adapter_id in first_by_id
    )


def test_builtin_json_parsers_returns_two_independent_adapters() -> None:
    first = builtin_json_parsers()
    second = builtin_json_parsers()
    first_by_id = {parser.adapter_id: parser for parser in first}
    second_by_id = {parser.adapter_id: parser for parser in second}

    assert {adapter_id: type(parser) for adapter_id, parser in first_by_id.items()} == {
        "builtin.json": JsonDocumentParser,
        "builtin.json-lines": JsonLinesParser,
    }
    assert first_by_id.keys() == second_by_id.keys()
    assert all(
        first_by_id[adapter_id] is not second_by_id[adapter_id]
        for adapter_id in first_by_id
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "display_name", "media_type", "expected_adapter", "format_id"),
    (
        (b"", "empty.log", "text/x-log", "builtin.text", "txt"),
        (
            b"ordinary prose without structural markers\n",
            "claimed.md",
            "text/markdown",
            "builtin.text",
            "txt",
        ),
        (
            b"# Content wins over the extension\n",
            "claimed.txt",
            "text/plain",
            "builtin.markdown",
            "md",
        ),
        (
            b"2026-09-02 10:45:01 INFO first\n2026-09-02 10:45:02 ERROR second\n",
            "claimed.txt",
            "text/plain",
            "builtin.log",
            "log",
        ),
        (
            b'{"level":"INFO"}\n{"level":"ERROR"}\n',
            "claimed.log",
            "text/x-log",
            "builtin.text",
            "txt",
        ),
        (
            b"- level=INFO user=alice\n- level=ERROR user=bob\n",
            "notes.md",
            "text/markdown",
            "builtin.markdown",
            "md",
        ),
        (
            _UNKNOWN_KV_LINE * 2,
            "unknown-kv.txt",
            "text/plain",
            "builtin.text",
            "txt",
        ),
        (
            _OVERFLOW_KV_LINE + b"ordinary prose\n",
            "single-log-candidate.txt",
            "text/plain",
            "builtin.text",
            "txt",
        ),
        (
            b"```log\n"
            b"2026-09-02 10:45:01 INFO first\n"
            b"2026-09-02 10:45:02 ERROR second\n"
            b"```\n",
            "embedded-log.md",
            "text/markdown",
            "builtin.markdown",
            "md",
        ),
        (
            b"~~~text\n"
            b'127.0.0.1 - - [10/Oct/2000:13:55:36 -0700] "GET / HTTP/1.0" '
            b"200 1\n"
            b'127.0.0.2 - - [10/Oct/2000:13:55:37 -0700] "GET / HTTP/1.0" '
            b"200 2\n"
            b"~~~\n",
            "embedded-access-log.md",
            "text/markdown",
            "builtin.markdown",
            "md",
        ),
        (
            b"```\nlevel=INFO user=alice\nlevel=ERROR user=bob\n```\n",
            "embedded-kv.md",
            "text/markdown",
            "builtin.markdown",
            "md",
        ),
        (
            b"2026-09-02 10:45:01 INFO see [docs](https://example.invalid/1)\n"
            b"2026-09-02 10:45:02 ERROR see [docs](https://example.invalid/2)\n",
            "linked.log",
            "text/x-log",
            "builtin.log",
            "log",
        ),
        (
            b"Read [one](https://example.invalid/1) here.\n"
            b"Then [two](https://example.invalid/2) there.\n",
            "links.md",
            "text/markdown",
            "builtin.markdown",
            "md",
        ),
        (
            b"Read [one](https://example.invalid/1) here.\n"
            b"Then [two](https://example.invalid/2) there.\n",
            "links.txt",
            "text/plain",
            "builtin.text",
            "txt",
        ),
    ),
)
async def test_group_a_selection_uses_content_ownership_not_declared_hints(
    content: bytes,
    display_name: str,
    media_type: str,
    expected_adapter: str,
    format_id: str,
) -> None:
    source = source_for(
        content,
        display_name=display_name,
        media_type=media_type,
    )
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many(builtin_text_parsers())

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == expected_adapter
    assert selected.probe_result.format_id == format_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "display_name", "media_type", "expected_adapter", "format_id"),
    (
        (
            b'{"outer":{"items":[1,{"raw":true}]}}\n',
            "claimed.txt",
            "text/plain",
            "builtin.json",
            "json",
        ),
        (
            b"42\n",
            "scalar.txt",
            "text/plain",
            "builtin.json",
            "json",
        ),
        (
            b'{"level":"INFO","message":"first"}\n'
            b'{"level":"ERROR","message":"second"}\n',
            "events.log",
            "text/x-log",
            "builtin.json-lines",
            "jsonl",
        ),
        (
            b"[1,2]\n[3,4]\n",
            "rows.csv",
            "text/csv",
            "builtin.json-lines",
            "jsonl",
        ),
        (
            b'{"single":true}\n',
            "single.jsonl",
            "application/x-ndjson",
            "builtin.json",
            "json",
        ),
        (b"", "empty.json", "application/json", "builtin.text", "txt"),
    ),
    ids=(
        "json-document",
        "json-scalar",
        "json-lines-beats-log",
        "json-lines-beats-delimited",
        "single-line-jsonl-is-document",
        "empty-json-hint-is-text",
    ),
)
async def test_all_builtin_selection_assigns_json_family_by_content(
    content: bytes,
    display_name: str,
    media_type: str,
    expected_adapter: str,
    format_id: str,
) -> None:
    source = source_for(
        content,
        display_name=display_name,
        media_type=media_type,
    )
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many(
        (
            *builtin_text_parsers(),
            *builtin_delimited_parsers(),
            *builtin_json_parsers(),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == expected_adapter
    assert selected.probe_result.format_id == format_id
    assert {
        "PARSER_DECLARED_MIME_MISMATCH",
        "PARSER_EXTENSION_MISMATCH",
    } <= set(selected.probe_result.warnings)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    (b"[TODO] write docs\n", b"1\ntest\n"),
    ids=("bracketed-prose", "one-scalar-line"),
)
async def test_weak_json_like_text_does_not_abort_builtin_selection(
    content: bytes,
) -> None:
    source = source_for(content, display_name="notes.txt", media_type="text/plain")
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many(
        (
            *builtin_text_parsers(),
            *builtin_delimited_parsers(),
            *builtin_json_parsers(),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "builtin.text"
    assert selected.probe_result.format_id == "txt"
