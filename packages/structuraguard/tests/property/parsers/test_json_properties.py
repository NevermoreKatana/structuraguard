"""Property и boundary invariants для JSON/JSONL physical extraction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    source_for,
)

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedTreeNode,
    JsonPointerLocation,
    StringScalar,
)
from structuraguard.parsers.builtin import (
    JsonDocumentParser,
    JsonLinesParser,
    JsonParserLimits,
)

type JsonValue = bool | int | str | list["JsonValue"] | dict[str, "JsonValue"] | None

_JSON_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"),
        blacklist_characters=("\x00", "\r"),
    ),
    max_size=16,
)
_JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    _JSON_TEXT,
)
_JSON_VALUES: st.SearchStrategy[JsonValue] = st.recursive(
    _JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(_JSON_TEXT, children, max_size=4),
    ),
    max_leaves=12,
)


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _expected_nodes(
    value: JsonValue,
    *,
    pointer: str = "",
) -> Iterator[tuple[str, str, object | None]]:
    if isinstance(value, dict):
        yield pointer, "object", None
        for key, child in value.items():
            child_pointer = f"{pointer}/{_escape_pointer_token(key)}"
            yield from _expected_nodes(child, pointer=child_pointer)
        return
    if isinstance(value, list):
        yield pointer, "array", None
        for index, child in enumerate(value):
            yield from _expected_nodes(child, pointer=f"{pointer}/{index}")
        return
    if value is None:
        yield pointer, "scalar", None
        return
    if type(value) is bool:
        yield pointer, "scalar", value
        return
    if type(value) is int:
        yield pointer, "scalar", str(value)
        return
    yield pointer, "scalar", value


def _tree_projection(
    batches: tuple[ExtractedBatch, ...],
) -> tuple[
    tuple[
        int | None,
        str,
        tuple[int | None, ...] | None,
        str,
        str | None,
        object | None,
    ],
    ...,
]:
    projection: list[
        tuple[
            int | None,
            str,
            tuple[int | None, ...] | None,
            str,
            str | None,
            object | None,
        ]
    ] = []
    for batch in batches:
        for node in batch.trees:
            location = node.location
            assert isinstance(location, JsonPointerLocation)
            raw_value: object | None = None
            if node.value is not None:
                raw_value = node.value.raw_value.value
            projection.append(
                (
                    location.record_index,
                    location.pointer,
                    location.occurrence_path,
                    str(node.node_kind),
                    node.raw_name,
                    raw_value,
                )
            )
    return tuple(projection)


def _json_location(node: ExtractedTreeNode) -> JsonPointerLocation:
    location = node.location
    assert isinstance(location, JsonPointerLocation)
    return location


async def _parse_document(
    content: bytes,
    *,
    batch_size: int = 1_000,
    max_chunk_bytes: int | None = None,
    limits: JsonParserLimits | None = None,
) -> tuple[ExtractedBatch, ...]:
    source = source_for(
        content,
        display_name="generated.json",
        media_type="application/json",
    )
    reader = (
        ShortReadSourceReader(
            source.source_fingerprint,
            content,
            max_chunk_bytes=max_chunk_bytes,
        )
        if max_chunk_bytes is not None
        else None
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=batch_size,
        reader=reader,
        detected_encoding="utf-8",
    )
    return await collect(JsonDocumentParser(limits=limits), source, context)


async def _parse_lines(
    content: bytes,
    *,
    suffix: str,
    batch_size: int,
    max_chunk_bytes: int | None,
) -> tuple[ExtractedBatch, ...]:
    source = source_for(
        content,
        display_name=f"generated.{suffix}",
        media_type=(
            "application/x-ndjson" if suffix == "ndjson" else "application/jsonl"
        ),
    )
    reader = (
        ShortReadSourceReader(
            source.source_fingerprint,
            content,
            max_chunk_bytes=max_chunk_bytes,
        )
        if max_chunk_bytes is not None
        else None
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=batch_size,
        reader=reader,
        detected_encoding="utf-8",
    )
    return await collect(JsonLinesParser(), source, context)


@settings(max_examples=35, deadline=None)
@given(value=_JSON_VALUES)
def test_json_tree_preserves_unicode_structure_raw_values_and_pointers(
    value: JsonValue,
) -> None:
    content = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()

    batches = asyncio.run(_parse_document(content))

    actual = tuple(
        (pointer, node_kind, raw_value)
        for _record_index, pointer, _occurrences, node_kind, _raw_name, raw_value in (
            _tree_projection(batches)
        )
    )
    assert actual == tuple(_expected_nodes(value))
    assert batches[-1].manifest is not None
    expected_record_count = len(value) if isinstance(value, list) else 1
    assert batches[-1].manifest.record_count == expected_record_count


@settings(max_examples=25, deadline=None)
@given(
    values=st.lists(_JSON_TEXT, min_size=7, max_size=7),
    suffix=st.sampled_from(("jsonl", "ndjson")),
    max_chunk_bytes=st.integers(min_value=1, max_value=11),
)
def test_json_lines_short_reads_keep_three_three_one_batches_without_loss(
    values: list[str],
    suffix: str,
    max_chunk_bytes: int,
) -> None:
    records = tuple(
        {"index": index, "text": value} for index, value in enumerate(values)
    )
    content = (
        "\n".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            for record in records
        )
        + "\n"
    ).encode()

    fragmented = asyncio.run(
        _parse_lines(
            content,
            suffix=suffix,
            batch_size=3,
            max_chunk_bytes=max_chunk_bytes,
        )
    )
    unfragmented = asyncio.run(
        _parse_lines(
            content,
            suffix=suffix,
            batch_size=3,
            max_chunk_bytes=None,
        )
    )

    assert tuple(batch.record_count for batch in fragmented) == (3, 3, 1)
    assert _tree_projection(fragmented) == _tree_projection(unfragmented)
    roots = tuple(
        location
        for batch in fragmented
        for node in batch.trees
        if isinstance((location := node.location), JsonPointerLocation)
        if node.parent_id is None
    )
    assert len(roots) == 7
    assert tuple(location.record_index for location in roots) == tuple(range(7))
    assert tuple(location.line_start for location in roots) == tuple(range(1, 8))
    assert tuple(location.line_end for location in roots) == tuple(range(1, 8))
    assert fragmented[-1].manifest is not None
    assert fragmented[-1].manifest.record_count == 7


def test_duplicate_keys_pointer_occurrences_and_number_lexemes_are_lossless() -> None:
    content = b'{"":-0,"a/b~c":1.2300e+04,"dup":1,"dup":2}'

    batches = asyncio.run(_parse_document(content, max_chunk_bytes=2))
    nodes = tuple(node for batch in batches for node in batch.trees)

    escaped = next(
        node
        for node in nodes
        if isinstance(node.location, JsonPointerLocation)
        and node.location.pointer == "/a~1b~0c"
    )
    assert escaped.raw_name == "a/b~c"
    assert escaped.value is not None
    assert isinstance(escaped.value.raw_value, StringScalar)
    assert escaped.value.raw_value.value == "1.2300e+04"

    empty_key = next(
        node
        for node in nodes
        if isinstance(node.location, JsonPointerLocation)
        and node.location.pointer == "/"
    )
    assert empty_key.raw_name == ""
    assert empty_key.value is not None
    assert empty_key.value.raw_value.value == "-0"

    duplicates = tuple(
        node
        for node in nodes
        if isinstance(node.location, JsonPointerLocation)
        and node.location.pointer == "/dup"
    )
    assert tuple(node.raw_name for node in duplicates) == ("dup", "dup")
    assert tuple(node.value.raw_value.value for node in duplicates if node.value) == (
        "1",
        "2",
    )
    assert len({_json_location(node).occurrence_path for node in duplicates}) == 2


def test_top_level_scalar_is_one_root_record_without_synthetic_container() -> None:
    batches = asyncio.run(_parse_document(b"-0"))
    nodes = tuple(node for batch in batches for node in batch.trees)

    assert len(nodes) == 1
    root = nodes[0]
    assert root.parent_id is None
    assert root.raw_name is None
    assert str(root.node_kind) == "scalar"
    assert isinstance(root.location, JsonPointerLocation)
    assert root.location.pointer == ""
    assert root.location.record_index is None
    assert root.value is not None
    assert root.value.raw_value.value == "-0"
    assert tuple(batch.record_count for batch in batches) == (1,)


def test_top_level_array_batch_boundary_has_no_duplicate_or_missing_items() -> None:
    content = json.dumps(
        [{"id": index} for index in range(7)],
        separators=(",", ":"),
    ).encode()

    batches = asyncio.run(_parse_document(content, batch_size=3, max_chunk_bytes=2))

    assert tuple(batch.record_count for batch in batches) == (3, 3, 1)
    expected_item_pointers = tuple(f"/{index}" for index in range(7))
    item_nodes = tuple(
        node
        for batch in batches
        for node in batch.trees
        if isinstance(node.location, JsonPointerLocation)
        and node.location.pointer in expected_item_pointers
    )
    assert tuple(_json_location(node).pointer for node in item_nodes) == (
        expected_item_pointers
    )
    assert len({_json_location(node).pointer for node in item_nodes}) == 7

    roots_by_batch = tuple(
        next(
            node
            for node in batch.trees
            if isinstance(node.location, JsonPointerLocation)
            and node.location.pointer == ""
        )
        for batch in batches
    )
    assert all(root.tree_id is not None for root in roots_by_batch)
    assert len({root.tree_id for root in roots_by_batch}) == 1
    assert all(
        item.parent_id == roots_by_batch[index // 3].node_id
        for index, item in enumerate(item_nodes)
    )


@settings(max_examples=20, deadline=None)
@given(batch_size=st.integers(min_value=1, max_value=7))
def test_top_array_projection_is_invariant_to_batch_size(batch_size: int) -> None:
    content = b'[{"nested":[0,{"raw":"\xd0\x9c\xd0\xb8\xd1\x80"}]},null,true,4]'

    batched = asyncio.run(_parse_document(content, batch_size=batch_size))
    baseline = asyncio.run(_parse_document(content, batch_size=100))

    batched_without_continuation_roots = tuple(
        item for item in _tree_projection(batched) if item[1] != ""
    )
    baseline_without_continuation_roots = tuple(
        item for item in _tree_projection(baseline) if item[1] != ""
    )
    assert batched_without_continuation_roots == baseline_without_continuation_roots
