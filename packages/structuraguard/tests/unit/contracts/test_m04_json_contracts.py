"""Контракты lossless JSON tree extraction milestone M4."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedDatasetManifest,
    ExtractedSourceIndex,
    ExtractedTreeNode,
    ExtractedValue,
    JsonPointerLocation,
    PhysicalNodeKind,
    ProducerMetadata,
    SourceArtifactRef,
    StringScalar,
)

SOURCE_FINGERPRINT = "sha256:" + "a" * 64
EXTRACTION_FINGERPRINT = "sha256:" + "e" * 64
OPTIONS_FINGERPRINT = "sha256:" + "f" * 64


def _source() -> SourceArtifactRef:
    return SourceArtifactRef(
        artifact_id="source-json",
        source_fingerprint=SOURCE_FINGERPRINT,
    )


def _location(
    pointer: str,
    *,
    record_index: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    column_start: int | None = None,
    column_end: int | None = None,
    occurrence_path: tuple[int | None, ...] | None = None,
) -> JsonPointerLocation:
    return JsonPointerLocation(
        source=_source(),
        pointer=pointer,
        record_index=record_index,
        line_start=line_start,
        line_end=line_end,
        column_start=column_start,
        column_end=column_end,
        occurrence_path=occurrence_path,
    )


def _scalar_node(
    *,
    node_id: str,
    parent_id: str | None,
    order: int,
    pointer: str,
    text: str,
    occurrence_path: tuple[int | None, ...],
) -> ExtractedTreeNode:
    location = _location(pointer, record_index=0, occurrence_path=occurrence_path)
    return ExtractedTreeNode(
        node_id=node_id,
        parent_id=parent_id,
        name="item",
        order=order,
        location=location,
        node_kind=PhysicalNodeKind.SCALAR,
        value=ExtractedValue(
            raw_value=StringScalar(value=text),
            location=location,
            technical_type_hint="json.string",
        ),
    )


def _array_segment(
    *,
    segment_index: int,
    child_start_index: int,
    values: tuple[str, ...],
    is_last_segment: bool,
) -> tuple[ExtractedTreeNode, ...]:
    root = ExtractedTreeNode(
        node_id="node-0",
        name="root",
        order=0,
        location=_location("", record_index=0, occurrence_path=()),
        node_kind=PhysicalNodeKind.ARRAY,
        tree_id="tree-0",
        segment_index=segment_index,
        child_start_index=child_start_index,
        child_count=len(values),
        is_last_segment=is_last_segment,
    )
    children = tuple(
        _scalar_node(
            node_id=f"node-{child_start_index + offset + 1}",
            parent_id=root.node_id,
            order=child_start_index + offset,
            pointer=f"/{child_start_index + offset}",
            text=value,
            occurrence_path=(None,),
        )
        for offset, value in enumerate(values)
    )
    return (root, *children)


def _batch(
    *,
    batch_index: int,
    fingerprint_digit: str,
    trees: tuple[ExtractedTreeNode, ...],
    record_count: int,
    is_last: bool = False,
    manifest: ExtractedDatasetManifest | None = None,
    schema_version: str = "1.1.0",
) -> ExtractedBatch:
    return ExtractedBatch(
        schema_version=schema_version,
        extraction_id="extraction-1",
        batch_index=batch_index,
        source=_source(),
        parser_id="parser.json",
        parser_version="1.0.0",
        batch_fingerprint="sha256:" + fingerprint_digit * 64,
        trees=trees,
        record_count=record_count if schema_version == "1.1.0" else None,
        is_last=is_last,
        manifest=manifest,
    )


def _manifest(
    batches: tuple[ExtractedBatch, ...],
    *,
    record_count: int,
) -> ExtractedDatasetManifest:
    return ExtractedDatasetManifest(
        schema_version="1.1.0",
        source=_source(),
        extraction_id="extraction-1",
        parser_id="parser.json",
        parser_version="1.0.0",
        producer=ProducerMetadata(
            component_id="parser.json",
            component_version="1.0.0",
            sdk_version="0.3.0",
        ),
        parser_options_fingerprint=OPTIONS_FINGERPRINT,
        batches=tuple(batch.to_summary() for batch in batches),
        extraction_fingerprint=EXTRACTION_FINGERPRINT,
        source_index=ExtractedSourceIndex(),
        record_count=record_count,
    )


def test_json_pointer_preserves_record_span_and_occurrence_path() -> None:
    location = _location(
        "/a~1b/~0/0",
        record_index=7,
        line_start=9,
        line_end=9,
        column_start=3,
        column_end=18,
        occurrence_path=(1, 0, None),
    )

    assert location.record_index == 7
    assert location.occurrence_path == (1, 0, None)
    assert (
        JsonPointerLocation.model_validate_json(location.model_dump_json()) == location
    )


@pytest.mark.parametrize(
    "factory",
    (
        lambda: _location("/invalid~"),
        lambda: _location("/invalid~2"),
        lambda: _location("/a/b", occurrence_path=(0,)),
        lambda: _location("", occurrence_path=(0,)),
        lambda: _location("/a", line_start=1),
        lambda: _location("/a", line_start=2, line_end=1),
        lambda: _location("/a", column_start=0, column_end=1),
        lambda: _location(
            "/a",
            line_start=1,
            line_end=1,
            column_start=2,
            column_end=1,
        ),
        lambda: _location("/a", occurrence_path=(True,)),
    ),
)
def test_json_pointer_rejects_invalid_escape_span_or_occurrence_alignment(
    factory: Callable[[], JsonPointerLocation],
) -> None:
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize(
    "raw_name",
    (
        "",
        "   ",
        "line\nfeed",
        "password=secret-token-value",
        "ключ/с~тильда",
    ),
)
def test_tree_raw_name_is_lossless_untrusted_text(raw_name: str) -> None:
    node = ExtractedTreeNode(
        node_id="node-1",
        parent_id="node-0",
        name="member",
        raw_name=raw_name,
        order=0,
        location=_location("/key", occurrence_path=(0,)),
        node_kind=PhysicalNodeKind.SCALAR,
        value=ExtractedValue(
            raw_value=StringScalar(value="value"),
            location=_location("/key", occurrence_path=(0,)),
        ),
    )

    assert node.raw_name == raw_name


def test_tree_raw_name_has_one_mebibyte_hard_cap() -> None:
    accepted = ExtractedTreeNode(
        node_id="node-0",
        name="root",
        raw_name="x" * 1_048_576,
        order=0,
        location=_location(""),
    )
    assert len(accepted.raw_name or "") == 1_048_576

    with pytest.raises(ValidationError):
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            raw_name="x" * 1_048_577,
            order=0,
            location=_location(""),
        )


@pytest.mark.parametrize(
    "node",
    (
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            order=0,
            location=_location(""),
            value=ExtractedValue(
                raw_value=StringScalar(value="value"),
                location=_location(""),
            ),
        ).model_copy(update={"node_kind": PhysicalNodeKind.OBJECT}),
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            order=0,
            location=_location(""),
        ).model_copy(update={"node_kind": PhysicalNodeKind.SCALAR}),
    ),
)
def test_tree_node_kind_binds_scalar_value_presence(node: ExtractedTreeNode) -> None:
    with pytest.raises(ValidationError):
        ExtractedTreeNode.model_validate(node.model_dump())


@pytest.mark.parametrize(
    "update",
    (
        {"tree_id": "tree-0"},
        {
            "tree_id": "tree-0",
            "segment_index": 0,
            "child_start_index": 0,
            "child_count": 0,
        },
    ),
)
def test_tree_continuation_fields_are_all_or_none(update: dict[str, object]) -> None:
    payload = {
        "node_id": "node-0",
        "name": "root",
        "order": 0,
        "location": _location(""),
        "node_kind": PhysicalNodeKind.ARRAY,
        **update,
    }

    with pytest.raises(ValidationError):
        ExtractedTreeNode.model_validate(payload)


def test_tree_continuation_is_allowed_only_on_container_root() -> None:
    valid = ExtractedTreeNode(
        node_id="node-0",
        name="root",
        order=0,
        location=_location(""),
        node_kind=PhysicalNodeKind.ARRAY,
        tree_id="tree-0",
        segment_index=0,
        child_start_index=0,
        child_count=0,
        is_last_segment=True,
    )
    assert valid.child_count == 0

    with pytest.raises(ValidationError):
        ExtractedTreeNode(
            node_id="node-1",
            parent_id="node-0",
            name="member",
            order=0,
            location=_location("/a"),
            node_kind=PhysicalNodeKind.OBJECT,
            tree_id="tree-0",
            segment_index=0,
            child_start_index=0,
            child_count=0,
            is_last_segment=True,
        )


def test_legacy_schema_rejects_json_tree_extensions() -> None:
    legacy_node = ExtractedTreeNode(
        node_id="node-0",
        name="root",
        order=0,
        location=_location(""),
    )
    legacy = _batch(
        batch_index=0,
        fingerprint_digit="1",
        trees=(legacy_node,),
        record_count=0,
        schema_version="1.0.0",
    )
    assert "node_kind" not in json.loads(legacy.canonical_json())["trees"][0]

    extended_node = legacy_node.model_copy(
        update={"node_kind": PhysicalNodeKind.OBJECT}
    )
    with pytest.raises(ValidationError, match=r"Schema 1\.0\.0"):
        _batch(
            batch_index=0,
            fingerprint_digit="2",
            trees=(extended_node,),
            record_count=0,
            schema_version="1.0.0",
        )


@pytest.mark.parametrize(
    "extended_node",
    (
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            raw_name="",
            order=0,
            location=_location(""),
        ),
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            order=0,
            location=_location("", record_index=0),
        ),
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            order=0,
            location=_location("", occurrence_path=()),
        ),
        ExtractedTreeNode(
            node_id="node-0",
            name="root",
            order=0,
            location=_location(
                "",
                line_start=1,
                line_end=1,
                column_start=0,
                column_end=0,
            ),
        ),
    ),
)
def test_legacy_schema_rejects_each_non_default_json_extension(
    extended_node: ExtractedTreeNode,
) -> None:
    with pytest.raises(ValidationError, match=r"Schema 1\.0\.0"):
        _batch(
            batch_index=0,
            fingerprint_digit="2",
            trees=(extended_node,),
            record_count=0,
            schema_version="1.0.0",
        )


def test_schema_1_1_accepts_lossless_json_tree_extensions() -> None:
    trees = _array_segment(
        segment_index=0,
        child_start_index=0,
        values=("raw",),
        is_last_segment=True,
    )
    batch = _batch(
        batch_index=0,
        fingerprint_digit="3",
        trees=trees,
        record_count=1,
    )

    payload = json.loads(batch.canonical_json())
    assert payload["trees"][0]["node_kind"] == "array"
    assert payload["trees"][0]["tree_id"] == "tree-0"
    assert payload["trees"][1]["value"]["raw_value"]["value"] == "raw"


@pytest.mark.parametrize(
    "trees",
    (
        _array_segment(
            segment_index=0,
            child_start_index=0,
            values=("only-one",),
            is_last_segment=True,
        )[:1],
        (
            *_array_segment(
                segment_index=0,
                child_start_index=0,
                values=("zero", "one"),
                is_last_segment=True,
            )[:2],
            _scalar_node(
                node_id="node-3",
                parent_id="node-0",
                order=2,
                pointer="/2",
                text="two",
                occurrence_path=(None,),
            ),
        ),
    ),
)
def test_tree_segment_child_count_and_range_match_direct_children(
    trees: tuple[ExtractedTreeNode, ...],
) -> None:
    with pytest.raises(ValidationError):
        _batch(
            batch_index=0,
            fingerprint_digit="4",
            trees=trees,
            record_count=1,
        )


def test_tree_segment_validation_does_not_materialize_declared_child_range() -> None:
    root = ExtractedTreeNode(
        node_id="node-0",
        name="root",
        order=0,
        location=_location(""),
        node_kind=PhysicalNodeKind.ARRAY,
        tree_id="tree-0",
        segment_index=0,
        child_start_index=0,
        child_count=10**12,
        is_last_segment=True,
    )

    with pytest.raises(ValidationError, match="child range"):
        _batch(
            batch_index=0,
            fingerprint_digit="4",
            trees=(root,),
            record_count=0,
        )


def test_manifest_accepts_consecutive_closed_tree_segments() -> None:
    first = _batch(
        batch_index=0,
        fingerprint_digit="5",
        trees=_array_segment(
            segment_index=0,
            child_start_index=0,
            values=("zero", "one"),
            is_last_segment=False,
        ),
        record_count=2,
    )
    terminal_draft = _batch(
        batch_index=1,
        fingerprint_digit="6",
        trees=_array_segment(
            segment_index=1,
            child_start_index=2,
            values=("two",),
            is_last_segment=True,
        ),
        record_count=1,
    )
    manifest = _manifest((first, terminal_draft), record_count=3)
    terminal = _batch(
        batch_index=1,
        fingerprint_digit="6",
        trees=terminal_draft.trees,
        record_count=1,
        is_last=True,
        manifest=manifest,
    )

    manifest.validate_batches((first, terminal))


@pytest.mark.parametrize(
    ("second_segment", "expected_message"),
    (
        (
            _array_segment(
                segment_index=2,
                child_start_index=1,
                values=("one",),
                is_last_segment=True,
            ),
            "Tree segments должны образовывать непрерывную sequence",
        ),
        (
            _array_segment(
                segment_index=1,
                child_start_index=2,
                values=("two",),
                is_last_segment=True,
            ),
            "Tree segments должны образовывать непрерывную sequence",
        ),
    ),
)
def test_manifest_rejects_tree_segment_index_or_child_range_gap(
    second_segment: tuple[ExtractedTreeNode, ...],
    expected_message: str,
) -> None:
    first = _batch(
        batch_index=0,
        fingerprint_digit="7",
        trees=_array_segment(
            segment_index=0,
            child_start_index=0,
            values=("zero",),
            is_last_segment=False,
        ),
        record_count=1,
    )
    terminal_draft = _batch(
        batch_index=1,
        fingerprint_digit="8",
        trees=second_segment,
        record_count=1,
    )
    manifest = _manifest((first, terminal_draft), record_count=2)
    terminal = _batch(
        batch_index=1,
        fingerprint_digit="8",
        trees=second_segment,
        record_count=1,
        is_last=True,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match=expected_message):
        manifest.validate_batches((first, terminal))


def test_manifest_rejects_unclosed_tree_segment() -> None:
    draft = _batch(
        batch_index=0,
        fingerprint_digit="9",
        trees=_array_segment(
            segment_index=0,
            child_start_index=0,
            values=("zero",),
            is_last_segment=False,
        ),
        record_count=1,
    )
    manifest = _manifest((draft,), record_count=1)
    terminal = _batch(
        batch_index=0,
        fingerprint_digit="9",
        trees=draft.trees,
        record_count=1,
        is_last=True,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="Segmented physical tree требует terminal"):
        manifest.validate_batches((terminal,))


def test_manifest_rejects_tree_segment_after_terminal_segment() -> None:
    first = _batch(
        batch_index=0,
        fingerprint_digit="a",
        trees=_array_segment(
            segment_index=0,
            child_start_index=0,
            values=("zero",),
            is_last_segment=True,
        ),
        record_count=1,
    )
    terminal_draft = _batch(
        batch_index=1,
        fingerprint_digit="b",
        trees=_array_segment(
            segment_index=1,
            child_start_index=1,
            values=("one",),
            is_last_segment=True,
        ),
        record_count=1,
    )
    manifest = _manifest((first, terminal_draft), record_count=2)
    terminal = _batch(
        batch_index=1,
        fingerprint_digit="b",
        trees=terminal_draft.trees,
        record_count=1,
        is_last=True,
        manifest=manifest,
    )

    with pytest.raises(ValueError, match="следует после terminal segment"):
        manifest.validate_batches((first, terminal))
