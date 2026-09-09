"""Additive markup fields не меняют legacy wire contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.unit.parsers.builtin._support import source_for

from structuraguard.contracts import (
    CssSelectorLocation,
    ExtractedBatch,
    ExtractedTreeNode,
    ExtractedValue,
    PhysicalMetadataEntry,
    PhysicalNodeKind,
    StringScalar,
)


def test_markup_fields_are_optional_in_legacy_node() -> None:
    source = source_for(b"", display_name="a.html")
    location = CssSelectorLocation(source=source.ref, selector=":scope")
    node = ExtractedTreeNode(node_id="node-1", name="root", order=0, location=location)
    data = node.model_dump(mode="json")
    assert "metadata" not in data and "raw_lexeme" not in data
    assert "node_index" not in location.model_dump()


@pytest.mark.parametrize(
    "kind",
    [
        PhysicalNodeKind.ELEMENT,
        PhysicalNodeKind.DOCUMENT,
        PhysicalNodeKind.MAPPING,
        PhysicalNodeKind.SEQUENCE,
    ],
)
def test_containers_cannot_claim_scalar_value(kind: PhysicalNodeKind) -> None:
    source = source_for(b"", display_name="a.html")
    location = CssSelectorLocation(source=source.ref, selector=":scope")
    with pytest.raises(ValidationError):
        ExtractedTreeNode(
            node_id="node-1",
            name="root",
            order=0,
            location=location,
            node_kind=kind,
            value=ExtractedValue(
                raw_value=StringScalar(value="raw"), location=location
            ),
        )


def test_new_fields_require_schema_11_and_unique_metadata() -> None:
    source = source_for(b"", display_name="a.html")
    location = CssSelectorLocation(
        source=source.ref,
        selector=":scope",
        node_index=0,
        line_number=1,
        column_number=0,
    )
    node = ExtractedTreeNode(
        node_id="node-1", name="root", order=0, location=location, raw_lexeme="<p>"
    )
    with pytest.raises(ValidationError):
        ExtractedBatch(
            schema_version="1.0.0",
            extraction_id="extraction-1",
            source=source.ref,
            parser_id="builtin.html",
            parser_version="1.0.0",
            batch_index=0,
            batch_fingerprint="sha256:" + "0" * 64,
            trees=(node,),
        )
    with pytest.raises(ValidationError):
        ExtractedTreeNode(
            node_id="node-1",
            name="root",
            order=0,
            location=location,
            metadata=(
                PhysicalMetadataEntry(key="x", value=1),
                PhysicalMetadataEntry(key="x", value=2),
            ),
        )
    with pytest.raises(ValidationError):
        CssSelectorLocation(source=source.ref, selector=":scope", column_number=0)
