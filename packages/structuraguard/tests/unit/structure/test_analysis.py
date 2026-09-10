"""Детерминированные планы на physical output настоящих technical parsers."""

from decimal import Decimal

import pytest
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts.analysis import ExplicitRecordGrouping
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    LogParsePlan,
    StructureAnalysisResult,
    StructureNeedsReview,
    StructurePlanCreated,
    TabularParsePlan,
    TreeNodeGrouping,
    TreeParsePlan,
)
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    DocxParser,
    JsonDocumentParser,
    LogParser,
    PdfParser,
    PlainTextParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.structure import (
    DeterministicStructureAnalyzer,
    StructureAnalysisOptions,
)


async def analyze_content(
    parser: Parser, content: bytes, *, options: StructureAnalysisOptions | None = None
) -> StructureAnalysisResult:
    source = source_for(content, display_name="sample")
    context = contexts_for(source, content, batch_size=2)[1]
    return await DeterministicStructureAnalyzer(options=options).analyze(
        parser.parse(source, context)
    )


@pytest.mark.anyio
async def test_csv_meta_header_repeated_header_footer() -> None:
    result = await analyze_content(
        DelimitedTextParser(),
        b"Report\nname,amount\nAda,12\nBob,20\nname,amount\nCara,4\nTotal,36\n",
    )
    assert isinstance(result, StructurePlanCreated)
    plan = result.plan
    assert isinstance(plan, TabularParsePlan)
    assert (plan.header_row, plan.data_start_row, plan.data_end_row) == (1, 2, 5)
    assert plan.footer_start_row == 6
    assert plan.repeated_header_rows == (4,)
    assert plan.schema_version == "1.1.0"
    assert plan.source_fingerprint == result.profile.source.source_fingerprint
    assert all(field.semantic_type == "unresolved" for field in plan.fields)
    assert plan.analysis is not None
    assert plan.analysis.unresolved_fields == tuple(f.field_id for f in plan.fields)
    assert TabularParsePlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.anyio
async def test_nested_json_child_collections_use_literal_steps() -> None:
    result = await analyze_content(
        JsonDocumentParser(),
        b'[{"id":1,"line items":[{"sku":"a"},{"sku":"b"}]},'
        b'{"id":2,"line items":[{"sku":"c"}]}]',
    )
    assert isinstance(result, StructurePlanCreated)
    plan = result.plan
    assert isinstance(plan, TreeParsePlan)
    assert len(plan.entities) == 2
    root, child = plan.entities
    assert child.parent_entity_id == root.entity_id
    assert isinstance(child.grouping, TreeNodeGrouping)
    assert any(step.name == "line items" for step in child.grouping.record_steps)
    assert all(f.semantic_type == "unresolved" for f in plan.fields)
    assert TreeParsePlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.anyio
async def test_log_uniform_multiline_and_mixed_variants() -> None:
    first = b"2026-09-10T12:00:00Z INFO user=1 started\n  detail one\n"
    second = b"2026-09-10T12:01:00Z ERROR user=2 failed\n  detail two\n"
    result = await analyze_content(LogParser(), first + second)
    assert isinstance(result, StructurePlanCreated)
    assert isinstance(result.plan, LogParsePlan)
    assert result.plan.max_lines_per_record == 2
    assert isinstance(result.plan.entities[0].grouping, ExplicitRecordGrouping)
    assert len(result.plan.entities[0].grouping.records) == 2
    assert result.plan.fields[0].selector.kind == "log_record"
    mixed = await analyze_content(
        LogParser(), first + b"metric cpu 42\nmetric cpu 43\n"
    )
    assert isinstance(mixed, StructureNeedsReview)
    assert len(mixed.candidates) >= 2
    assert mixed.candidates == tuple(
        sorted(
            mixed.candidates,
            key=lambda c: (c.confidence.copy_negate(), c.plan_kind, c.candidate_id),
        )
    )


@pytest.mark.anyio
@pytest.mark.integration
async def test_pdf_and_docx_physical_block_plans() -> None:
    parts = package_parts("docx")
    parts["word/document.xml"] = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b'<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Entry</w:t></w:r></w:p>'
        b"<w:p><w:r><w:t>Name: Ada</w:t></w:r></w:p>"
        b'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Entry</w:t></w:r></w:p>'
        b"<w:p><w:r><w:t>Name: Bob</w:t></w:r></w:p></w:body></w:document>"
    )
    for parser, content in (
        (PdfParser(), pdf_bytes(pages=2)),
        (DocxParser(), zip_bytes(parts)),
    ):
        result = await analyze_content(parser, content)
        assert isinstance(result, StructurePlanCreated)
        assert isinstance(result.plan, DocumentParsePlan)
        assert result.plan.block_refs
        assert isinstance(result.plan.entities[0].grouping, ExplicitRecordGrouping)
        assert all(record for record in result.plan.entities[0].grouping.records)
        assert (
            DocumentParsePlan.model_validate_json(result.plan.model_dump_json())
            == result.plan
        )


@pytest.mark.anyio
async def test_empty_unknown_ambiguous_and_threshold_are_explicit() -> None:
    for content in (b"", b"unstructured prose without repeated shape\n"):
        result = await analyze_content(PlainTextParser(), content)
        assert result.kind == "needs_semantic_analysis"
        assert result.code == "NEEDS_SEMANTIC_ANALYSIS"
    ambiguous = await analyze_content(
        DelimitedTextParser(), b"red,green\nblue,white\nblack,orange\n"
    )
    assert isinstance(ambiguous, StructureNeedsReview)
    assert len(ambiguous.candidates) >= 2
    high = await analyze_content(
        DelimitedTextParser(),
        b"name,count\nAda,1\nBob,2\n",
        options=StructureAnalysisOptions(confidence_threshold=Decimal("0.99")),
    )
    assert high.kind == "needs_semantic_analysis"


@pytest.mark.anyio
async def test_independent_nested_arrays_are_ranked_without_scope_loss() -> None:
    result = await analyze_content(
        JsonDocumentParser(), b'{"a":[{"id":1},{"id":2}],"b":[{"n":3},{"n":4}]}'
    )
    assert isinstance(result, StructureNeedsReview)
    assert len(result.candidates) == 2


@pytest.mark.anyio
async def test_tree_outer_scalar_context_is_preserved_as_parent() -> None:
    result = await analyze_content(
        JsonDocumentParser(), b'{"report":"weekly","items":[{"id":1},{"id":2}]}'
    )
    assert isinstance(result, StructurePlanCreated)
    assert isinstance(result.plan, TreeParsePlan)
    parent, child = result.plan.entities
    assert child.parent_entity_id == parent.entity_id
    assert len(parent.field_ids) == 1


@pytest.mark.anyio
async def test_ties_repeat_and_candidate_budget_never_selects_a_winner() -> None:
    content = b'{"a":[{"id":1},{"id":2}],"b":[{"id":3},{"id":4}]}'
    first = await analyze_content(JsonDocumentParser(), content)
    second = await analyze_content(JsonDocumentParser(), content)
    assert first == second
    assert isinstance(first, StructureNeedsReview)
    assert first.candidates[0].confidence == first.candidates[1].confidence
    limited = await analyze_content(
        JsonDocumentParser(),
        content,
        options=StructureAnalysisOptions(
            profiling=StructuralProfilingOptions(max_candidates=1)
        ),
    )
    assert limited.kind == "needs_semantic_analysis"
    assert limited.issues[0].code == "STRUCTURE_CANDIDATE_LIMIT"
