"""Chunk/batch invariance независимых markup grammars."""

from __future__ import annotations

import asyncio
from html import escape
from json import dumps

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    source_for,
)

from structuraguard.contracts import ExtractedBatch
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import HtmlParser, XmlParser, YamlParser
from structuraguard.ports import Parser

_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"), blacklist_characters=("\ufffe", "\uffff")
    ),
    max_size=16,
)


def projection(batches: tuple[ExtractedBatch, ...]) -> list[object]:
    nodes: dict[str, object] = {}
    for batch in batches:
        for node in batch.trees:
            nodes.setdefault(
                node.node_id,
                node.model_dump(
                    exclude={
                        "tree_id",
                        "segment_index",
                        "child_start_index",
                        "child_count",
                        "is_last_segment",
                    }
                ),
            )
    return [
        *nodes.values(),
        *(b.model_dump() for batch in batches for b in batch.blocks),
        *(t.model_dump() for batch in batches for t in batch.tables),
    ]


async def _compare(
    parser: Parser, content: bytes, batch_size: int, chunk_size: int
) -> None:
    source = source_for(content, display_name="a.txt")
    base = await collect(parser, source, contexts_for(source, content)[1])
    reader = ShortReadSourceReader(
        source.source_fingerprint, content, max_chunk_bytes=chunk_size
    )
    split = await collect(
        parser,
        source,
        contexts_for(source, content, batch_size=batch_size, reader=reader)[1],
    )
    assert projection(base) == projection(split)
    assert split[-1].manifest is not None
    split[-1].manifest.validate_batches(split)


@settings(max_examples=40, deadline=None)
@given(st.lists(_TEXT, min_size=1, max_size=8), st.integers(1, 4), st.integers(1, 17))
def test_xml_namespace_mixed_content_chunk_and_batch_invariance(
    values: list[str], batch_size: int, chunk_size: int
) -> None:
    content = (
        '<r xmlns:x="urn:property">before'
        + "".join(
            f'<x:item a="{escape(v, quote=True)}">{escape(v)}</x:item>tail'
            for v in values
        )
        + "</r>"
    ).encode()
    asyncio.run(_compare(XmlParser(), content, batch_size, chunk_size))


@settings(max_examples=40, deadline=None)
@given(st.lists(_TEXT, min_size=1, max_size=8), st.integers(1, 4), st.integers(1, 17))
def test_html_entities_and_dom_chunk_and_batch_invariance(
    values: list[str], batch_size: int, chunk_size: int
) -> None:
    content = "".join(
        f'<p data-x="{escape(v, quote=True)}">{escape(v)}</p>' for v in values
    ).encode()
    asyncio.run(_compare(HtmlParser(), content, batch_size, chunk_size))


@settings(max_examples=40, deadline=None)
@given(
    st.lists(_TEXT, min_size=1, max_size=8),
    st.integers(1, 4),
    st.integers(1, 17),
    st.sampled_from(["\n", "\r\n", "\r"]),
)
def test_yaml_documents_and_marks_chunk_and_batch_invariance(
    values: list[str], batch_size: int, chunk_size: int, newline: str
) -> None:
    content = "".join(
        f"---{newline}key: {dumps(v, ensure_ascii=True)}{newline}" for v in values
    ).encode()
    asyncio.run(_compare(YamlParser(), content, batch_size, chunk_size))


@settings(max_examples=100, deadline=None)
@given(st.text(alphabet="<>/!-[]'\"&;=abc: %{}\n\x00", max_size=80))
def test_malformed_markup_is_data_or_typed_error(text: str) -> None:
    async def check() -> None:
        content = text.encode()
        source = source_for(content, display_name="a.txt")
        for parser in (XmlParser(), HtmlParser(), YamlParser()):
            try:
                batches = await collect(
                    parser, source, contexts_for(source, content)[1]
                )
            except (ParserError, SecurityPolicyError):
                continue
            assert batches[-1].manifest is not None
            batches[-1].manifest.validate_batches(batches)

    asyncio.run(check())
