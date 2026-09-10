"""Явный caller-authored plan для форматов, не обещающих automatic inference."""

from decimal import Decimal

import pytest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import execute, stream

from structuraguard.contracts.analysis import (
    ExplicitRecordGrouping,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.common import PhysicalObjectKind, PhysicalSourceRef
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    DocumentTargetSelector,
    ParseEntity,
    ParseField,
    ParsePlanValidationRequest,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.parsers.builtin import HtmlParser, MarkdownParser, XmlParser
from structuraguard.ports.parser import Parser
from structuraguard.structure import StructuralProfiler


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "expected"),
    [
        (
            XmlParser(),
            b"<root><item><name>Ada</name></item><item><name>Bob</name></item></root>",
            ["Ada", "Bob"],
        ),
        (HtmlParser(), b"<p>Name: Ada</p><p>Name: Bob</p>", ["Name: Ada", "Name: Bob"]),
        (MarkdownParser(), b"Name: Ada\n\nName: Bob\n", ["Name: Ada\n", "Name: Bob\n"]),
    ],
    ids=["xml_occurrences", "html_blocks", "markdown_paragraphs"],
)
async def test_explicit_markup_scope_preserves_literal_paths_and_raw_text(
    parser: Parser,
    content: bytes,
    expected: list[str],
) -> None:
    source = source_for(content, display_name="explicit")
    batches = await collect(
        parser, source, contexts_for(source, content, batch_size=1)[1]
    )
    profile = await StructuralProfiler().profile(stream(batches))
    manifest = batches[-1].manifest
    assert manifest is not None
    plan: TreeParsePlan | DocumentParsePlan
    if isinstance(parser, XmlParser):
        root = next(
            ref
            for ref in manifest.source_index.refs
            if ref.kind is PhysicalObjectKind.TREE_NODE
        )
        fields = tuple(
            ParseField(
                field_id=f"name_{i}",
                semantic_name=f"name_{i}",
                semantic_type="unresolved",
                source_refs=(root,),
                selector=TreePathSelector(
                    steps=(
                        TreeStep(
                            operation=TreePathOperation.KEY, name="item", occurrence=i
                        ),
                        TreeStep(operation=TreePathOperation.KEY, name="name"),
                        TreeStep(operation=TreePathOperation.KEY, name="#text"),
                    )
                ),
            )
            for i in range(2)
        )
        plan = TreeParsePlan(
            plan_id="explicit_xml",
            schema_version="1.1.0",
            revision=1,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            producer=profile.producer,
            confidence=Decimal(1),
            root_ref=root,
            fields=fields,
            entities=(
                ParseEntity(
                    entity_id="document",
                    entity_type="unresolved",
                    field_ids=tuple(f.field_id for f in fields),
                    grouping=TreeNodeGrouping(),
                ),
            ),
            evidence=(root,),
        )
    else:
        refs = tuple(
            PhysicalSourceRef(
                extraction_id=manifest.extraction_id,
                batch_index=batch.batch_index,
                kind=PhysicalObjectKind.BLOCK,
                local_id=block.block_id,
            )
            for batch in batches
            for block in batch.blocks
            if block.kind.value == "paragraph"
        )
        plan = DocumentParsePlan(
            plan_id="explicit_document",
            schema_version="1.1.0",
            revision=1,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            producer=profile.producer,
            confidence=Decimal(1),
            block_refs=refs,
            fields=(
                ParseField(
                    field_id="text",
                    semantic_name="text",
                    semantic_type="unresolved",
                    source_refs=refs,
                    selector=DocumentTargetSelector(
                        target="block_text", block_offset=0
                    ),
                ),
            ),
            entities=(
                ParseEntity(
                    entity_id="paragraph",
                    entity_type="unresolved",
                    field_ids=("text",),
                    grouping=ExplicitRecordGrouping(
                        records=tuple((ref,) for ref in refs)
                    ),
                ),
            ),
            evidence=refs,
        )
    request = ParsePlanValidationRequest(
        plan=plan, source=manifest.source, manifest=manifest, profile=profile
    )
    output = await execute(request, batches)
    assert [
        v.raw_value.value
        for b in output
        for r in b.records
        for e in r.entities
        for v in e.values
    ] == expected
    physical = {ref for batch in batches for ref in batch.physical_refs()}
    assert all(
        ref in physical
        for batch in output
        for r in batch.records
        for ref in r.source_refs
    )
