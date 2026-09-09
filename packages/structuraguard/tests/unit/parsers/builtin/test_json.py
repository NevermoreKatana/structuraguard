"""Contract и boundary tests JSON/JSONL technical adapters."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    source_for,
)

from structuraguard.contracts import (
    BooleanScalar,
    ExtractedBatch,
    ExtractedTreeNode,
    JsonPointerLocation,
    NullScalar,
    PhysicalNodeKind,
    StringScalar,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import (
    JsonDocumentParser,
    JsonLinesParser,
    JsonParserLimits,
)


def _nodes(batches: tuple[ExtractedBatch, ...]) -> tuple[ExtractedTreeNode, ...]:
    return tuple(node for batch in batches for node in batch.trees)


def _logical_nodes(
    batches: tuple[ExtractedBatch, ...],
) -> tuple[ExtractedTreeNode, ...]:
    """Убрать только повторные continuation roots из physical projection."""

    seen_segmented_roots: set[str] = set()
    result: list[ExtractedTreeNode] = []
    for node in _nodes(batches):
        if node.segment_index is None:
            result.append(node)
            continue
        assert node.tree_id is not None
        if node.tree_id not in seen_segmented_roots:
            seen_segmented_roots.add(node.tree_id)
            result.append(node)
    return tuple(result)


def _json_location(node: ExtractedTreeNode) -> JsonPointerLocation:
    location = node.location
    assert isinstance(location, JsonPointerLocation)
    return location


@pytest.mark.anyio
async def test_json_contract_preserves_nested_tree_duplicates_and_raw_values() -> None:
    content = b'{"a/b":{"~key":1,"~key":1.0},"a/b":[true,null,{"":-0e+2}]}'
    source = source_for(
        content, display_name="nested.json", media_type="application/json"
    )
    probe_context, parse_context = contexts_for(source, content, batch_size=2)

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=JsonDocumentParser(),
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    nodes = _logical_nodes(batches)
    assert tuple(node.node_kind for node in nodes) == (
        PhysicalNodeKind.OBJECT,
        PhysicalNodeKind.OBJECT,
        PhysicalNodeKind.SCALAR,
        PhysicalNodeKind.SCALAR,
        PhysicalNodeKind.ARRAY,
        PhysicalNodeKind.SCALAR,
        PhysicalNodeKind.SCALAR,
        PhysicalNodeKind.OBJECT,
        PhysicalNodeKind.SCALAR,
    )
    assert tuple(node.raw_name for node in nodes) == (
        None,
        "a/b",
        "~key",
        "~key",
        "a/b",
        None,
        None,
        None,
        "",
    )
    locations = tuple(_json_location(node) for node in nodes)
    assert tuple(location.pointer for location in locations) == (
        "",
        "/a~1b",
        "/a~1b/~0key",
        "/a~1b/~0key",
        "/a~1b",
        "/a~1b/0",
        "/a~1b/1",
        "/a~1b/2",
        "/a~1b/2/",
    )
    assert tuple(location.occurrence_path for location in locations) == (
        (),
        (0,),
        (0, 0),
        (0, 1),
        (1,),
        (1, None),
        (1, None),
        (1, None),
        (1, None, 0),
    )

    scalar_values = tuple(node.value for node in nodes if node.value is not None)
    assert isinstance(scalar_values[0].raw_value, StringScalar)
    assert scalar_values[0].raw_value.value == "1"
    assert scalar_values[0].technical_type_hint == "json.integer"
    assert isinstance(scalar_values[1].raw_value, StringScalar)
    assert scalar_values[1].raw_value.value == "1.0"
    assert scalar_values[1].technical_type_hint == "json.number"
    assert isinstance(scalar_values[2].raw_value, BooleanScalar)
    assert isinstance(scalar_values[3].raw_value, NullScalar)
    assert isinstance(scalar_values[4].raw_value, StringScalar)
    assert scalar_values[4].raw_value.value == "-0e+2"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "scalar_type", "expected"),
    (
        (b'"text"', StringScalar, "text"),
        (b"42", StringScalar, "42"),
        (b"true", BooleanScalar, True),
        (b"null", NullScalar, None),
    ),
)
async def test_json_top_level_scalar_is_one_strict_tree_record(
    content: bytes,
    scalar_type: type[StringScalar | BooleanScalar | NullScalar],
    expected: str | bool | None,
) -> None:
    source = source_for(
        content, display_name="scalar.json", media_type="application/json"
    )
    _, context = contexts_for(source, content)

    batches = await collect(JsonDocumentParser(), source, context)

    assert len(batches) == 1
    assert batches[0].record_count == 1
    assert len(batches[0].trees) == 1
    node = batches[0].trees[0]
    assert node.node_kind is PhysicalNodeKind.SCALAR
    assert node.parent_id is None
    assert node.raw_name is None
    assert isinstance(node.location, JsonPointerLocation)
    assert node.location.pointer == ""
    assert node.value is not None
    assert isinstance(node.value.raw_value, scalar_type)
    assert node.value.raw_value.value == expected


@pytest.mark.anyio
async def test_json_lines_batch_boundary_has_no_duplicates_or_gaps() -> None:
    content = b"".join(f'{{"id":{index}}}\n'.encode() for index in range(7))
    source = source_for(
        content,
        display_name="records.ndjson",
        media_type="application/x-ndjson",
    )
    reader = ShortReadSourceReader(
        source.source_fingerprint, content, max_chunk_bytes=1
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=3,
        reader=reader,
        detected_encoding="utf-8",
    )

    batches = await collect(JsonLinesParser(), source, context)

    assert tuple(batch.record_count for batch in batches) == (3, 3, 1)
    roots = tuple(node for node in _nodes(batches) if node.parent_id is None)
    assert tuple(_json_location(root).record_index for root in roots) == tuple(range(7))
    assert tuple(_json_location(root).line_start for root in roots) == tuple(
        range(1, 8)
    )
    assert sum(batch.record_count or 0 for batch in batches) == 7
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 7


@pytest.mark.anyio
async def test_json_lines_trailing_blank_lines_do_not_add_terminal_batch() -> None:
    content = b"1\n \t\r\n\n"
    source = source_for(
        content,
        display_name="one-record.ndjson",
        media_type="application/x-ndjson",
    )
    _, context = contexts_for(
        source,
        content,
        batch_size=1,
        max_batches=1,
        detected_encoding="utf-8",
    )

    batches = await collect(JsonLinesParser(), source, context)

    assert len(batches) == 1
    assert batches[0].record_count == 1
    assert batches[0].is_last is True
    assert batches[0].manifest is not None
    assert batches[0].manifest.record_count == 1


@pytest.mark.anyio
async def test_top_level_array_segments_keep_each_item_exactly_once() -> None:
    content = b'[{"id":0},{"id":1},{"id":2},{"id":3},{"id":4}]'
    source = source_for(
        content, display_name="items.json", media_type="application/json"
    )
    _, context = contexts_for(source, content, batch_size=2)

    batches = await collect(JsonDocumentParser(), source, context)

    assert tuple(batch.record_count for batch in batches) == (2, 2, 1)
    roots = tuple(batch.trees[0] for batch in batches)
    assert tuple(root.segment_index for root in roots) == (0, 1, 2)
    assert tuple(root.child_start_index for root in roots) == (0, 2, 4)
    assert tuple(root.child_count for root in roots) == (2, 2, 1)
    assert tuple(root.is_last_segment for root in roots) == (False, False, True)
    assert len({root.tree_id for root in roots}) == 1
    item_roots = tuple(
        node
        for batch in batches
        for node in batch.trees[1:]
        if node.parent_id == batch.trees[0].node_id
    )
    assert tuple(node.order for node in item_roots) == tuple(range(5))
    assert tuple(_json_location(node).pointer for node in item_roots) == tuple(
        f"/{index}" for index in range(5)
    )


@pytest.mark.anyio
async def test_json_lines_malformed_record_reports_exact_physical_line() -> None:
    content = b'{"ok":1}\r\n{"broken":]\r\n{"unread":3}\r\n'
    source = source_for(
        content, display_name="broken.jsonl", media_type="application/jsonl"
    )
    _, context = contexts_for(source, content, batch_size=10)

    with pytest.raises(ParserError) as caught:
        await collect(JsonLinesParser(), source, context)

    assert caught.value.error_code == "PARSER_MALFORMED_INPUT"
    assert caught.value.details["line_number"] == 2
    assert caught.value.details["record_number"] == 2
    assert "broken" not in str(caught.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limits", "content", "resource"),
    (
        (JsonParserLimits(max_keys=1), b'{"a":1,"b":2}', "keys"),
        (JsonParserLimits(max_value_chars=3), b'{"a":"four"}', "value_chars"),
        (JsonParserLimits(max_number_chars=2), b'{"a":123}', "number_chars"),
        (JsonParserLimits(max_nodes=2), b'{"a":{"b":1}}', "nodes"),
    ),
)
async def test_json_format_limits_fail_with_typed_resource(
    limits: JsonParserLimits,
    content: bytes,
    resource: str,
) -> None:
    source = source_for(
        content, display_name="limited.json", media_type="application/json"
    )
    _, context = contexts_for(source, content)

    with pytest.raises(SecurityPolicyError) as caught:
        await collect(JsonDocumentParser(limits=limits), source, context)

    assert caught.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert caught.value.details["resource"] == resource


@pytest.mark.anyio
async def test_context_nesting_and_record_limits_apply_before_next_record() -> None:
    nested = b'{"a":{"b":1}}'
    source = source_for(
        nested, display_name="nested.json", media_type="application/json"
    )
    _, context = contexts_for(source, nested)

    with pytest.raises(SecurityPolicyError) as depth_error:
        await collect(
            JsonDocumentParser(),
            source,
            replace(context, max_nesting_depth=2),
        )
    assert depth_error.value.details["resource"] == "nesting_depth"

    lines = b"1\n2\n"
    lines_source = source_for(
        lines, display_name="two.jsonl", media_type="application/jsonl"
    )
    _, lines_context = contexts_for(lines_source, lines)
    with pytest.raises(SecurityPolicyError) as records_error:
        await collect(
            JsonLinesParser(),
            lines_source,
            replace(lines_context, max_records=1),
        )
    assert records_error.value.details["resource"] == "records"
