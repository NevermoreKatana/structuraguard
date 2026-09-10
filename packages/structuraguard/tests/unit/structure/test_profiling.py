"""Наблюдаемые contracts bounded structural profiling на настоящих parsers."""

from collections.abc import AsyncGenerator
from dataclasses import replace
from decimal import ROUND_UP, localcontext

import pytest
from tests.unit.parsers.builtin._support import contexts_for, source_for

from structuraguard.contracts import StructureProfile
from structuraguard.contracts.structure import (
    DocumentObservation,
    FieldObservation,
    StructuralProfilingOptions,
    TabularObservation,
    TextObservation,
    TreeObservation,
)
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    HtmlParser,
    JsonDocumentParser,
    PlainTextParser,
    XlsxParser,
    XmlParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.structure import StructuralProfiler


async def profile_content(
    parser: Parser,
    content: bytes,
    *,
    batch_size: int = 2,
    options: StructuralProfilingOptions | None = None,
) -> StructureProfile:
    """Передать одноразовый поток, не материализуя весь extracted source."""
    source = source_for(content, display_name="sample")
    context = replace(
        contexts_for(source, content, batch_size=batch_size)[1],
        max_records=100_000,
        max_physical_objects=1_000_000,
    )
    return await StructuralProfiler(options=options).profile(
        parser.parse(source, context)
    )


@pytest.mark.anyio
async def test_empty_source_has_explicit_empty_profile() -> None:
    profile = await profile_content(PlainTextParser(), b"")
    assert profile.schema_version == "1.1.0"
    assert profile.observations == profile.evidence == profile.candidates == ()
    assert profile.coverage is not None
    assert profile.coverage.sampled_items == 0
    assert profile.coverage.complete
    assert StructureProfile.model_validate_json(profile.model_dump_json()) == profile


@pytest.mark.anyio
async def test_tabular_headers_regions_raggedness_and_fields() -> None:
    content = b"name,amount\nAlice,12\nBob,20\nname,amount\nCara,4,extra\n\nTotal,36\n"
    profile = await profile_content(DelimitedTextParser(), content)
    tables = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, TabularObservation)
    ]
    assert any(t.role == "header" and t.row_start == 0 for t in tables)
    assert not any(t.role == "header" and t.row_start in {1, 2, 4} for t in tables)
    assert any(t.role == "repeated_header" and t.row_start == 3 for t in tables)
    assert any(t.role == "empty" and t.row_start == 5 for t in tables)
    assert any(t.role == "footer" and t.row_start == 6 for t in tables)
    assert any(
        t.role == "shape" and t.column_count == 2 and t.ragged_rows > 0 for t in tables
    )
    fields = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, FieldObservation)
    ]
    assert any(
        f.suggested_name == "amount" and "integer" in f.type_counts for f in fields
    )
    assert all(
        item.source_refs and 0 <= item.confidence <= 1 for item in profile.observations
    )


@pytest.mark.anyio
async def test_ambiguous_text_table_keeps_multiple_header_hypotheses() -> None:
    profile = await profile_content(
        DelimitedTextParser(), b"red,green\nblue,white\nblack,orange\n"
    )
    headers = [
        item
        for item in profile.observations
        if isinstance(item.observation, TabularObservation)
        and item.observation.role == "header"
    ]
    assert len(headers) >= 2
    assert len(profile.candidates) >= 2
    assert all(candidate.confidence < 1 for candidate in profile.candidates)


@pytest.mark.anyio
async def test_nested_tree_collections_preserve_parent_paths() -> None:
    profile = await profile_content(
        JsonDocumentParser(),
        b'[{"id":1,"items":[{"sku":"a"},{"sku":"b"}]},{"id":2,"items":[{"sku":"c"}]}]',
        batch_size=1,
    )
    trees = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, TreeObservation)
    ]
    assert any(t.role == "record_root" and t.occurrences >= 2 for t in trees)
    assert any(t.role == "collection" and t.parent_path is not None for t in trees)
    assert any(t.key_counts and "id" in t.key_counts for t in trees)
    assert any(t.array_count > 0 for t in trees)


@pytest.mark.anyio
async def test_text_templates_key_values_and_multiline_candidates() -> None:
    content = (
        b"2026-09-10T12:00:00Z INFO user=12 started\n  detail one\n\n"
        b"2026-09-10T12:01:00Z INFO user=34 started\n  detail two\n\n"
        b"2026-09-10T12:02:00Z ERROR user=56 failed\n"
    )
    profile = await profile_content(PlainTextParser(), content)
    text = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, TextObservation)
    ]
    assert any(t.role == "template" and t.occurrences == 2 for t in text)
    assert any(t.role == "key_value" and "user" in t.keys for t in text)
    assert any(t.role == "multiline" for t in text)
    assert any(t.timestamp_count and t.level_count for t in text)
    assert all("regex" not in type(t).model_fields for t in text)
    assert len(profile.candidates) > 1


@pytest.mark.anyio
async def test_mixed_document_preserves_multiple_families() -> None:
    content = b"<html><h1>Invoice</h1><p>Name: Ada</p><p>Age: 21</p><h1>Invoice</h1><p>Name: Bob</p><p>Age: 22</p><table><tr><th>item</th><th>count</th></tr><tr><td>pen</td><td>2</td></tr></table></html>"
    profile = await profile_content(HtmlParser(), content)
    observations = [item.observation for item in profile.observations]
    assert any(isinstance(item, TabularObservation) for item in observations)
    docs = [item for item in observations if isinstance(item, DocumentObservation)]
    assert {"heading", "section", "key_value", "table", "repeated_group"} <= {
        item.role for item in docs
    }
    assert len({candidate.plan_kind for candidate in profile.candidates}) >= 2


@pytest.mark.anyio
async def test_large_input_keeps_configured_sample_bound_and_determinism() -> None:
    options = StructuralProfilingOptions(max_sample_items=8, max_sample_bytes=4096)
    content = b"INFO id=123 completed\n" * 5_000
    first = await profile_content(
        PlainTextParser(), content, options=options, batch_size=127
    )
    second = await profile_content(
        PlainTextParser(), content, options=options, batch_size=127
    )
    assert first == second
    assert first.coverage is not None
    assert first.coverage.seen_items == 5_000
    assert first.coverage.sampled_items <= 8
    assert first.coverage.sampled_bytes <= 4096
    assert not first.coverage.complete
    assert first.coverage.skipped_items > 0
    assert len(first.canonical_json()) < 30_000


@pytest.mark.anyio
async def test_single_terminal_batch_is_accepted() -> None:
    source = source_for(b"one\ntwo\n", display_name="text")
    stream = PlainTextParser().parse(source, contexts_for(source, b"one\ntwo\n")[1])
    batch = await anext(stream)
    assert batch.is_last
    assert isinstance(stream, AsyncGenerator)
    await stream.aclose()
    profile = await StructuralProfiler().profile(batch)
    assert profile.evidence


@pytest.mark.anyio
async def test_late_footer_is_retained_without_unbounded_rows() -> None:
    content = b"name,amount\n" + b"Alice,10\n" * 200 + b"Total,2000\n"
    profile = await profile_content(
        DelimitedTextParser(),
        content,
        options=StructuralProfilingOptions(max_sample_items=8, tail_rows=2),
    )
    assert profile.coverage is not None
    assert profile.coverage.sampled_items <= 8
    assert profile.coverage.skipped_items >= 194
    footer = [
        item
        for item in profile.observations
        if isinstance(item.observation, TabularObservation)
        and item.observation.role == "footer"
    ]
    assert footer and isinstance(footer[0].observation, TabularObservation)
    assert footer[0].observation.row_start == 201
    assert all(ref.kind.value == "table" for ref in footer[0].source_refs)
    assert not profile.coverage.complete


@pytest.mark.anyio
async def test_prefix_gap_does_not_create_a_false_contiguous_line_region() -> None:
    profile = await profile_content(
        PlainTextParser(),
        b"short\n" + b"x" * 200 + b"\nshort\n",
        options=StructuralProfilingOptions(max_value_chars=32),
    )
    ranges = [
        (item.observation.line_start, item.observation.line_end)
        for item in profile.observations
        if isinstance(item.observation, TextObservation)
        and item.observation.role == "line_boundary"
    ]
    assert ranges == [(1, 1), (3, 3)]
    assert (
        profile.coverage is not None and "oversized_sample" in profile.coverage.reasons
    )


@pytest.mark.anyio
async def test_xml_repeated_siblings_and_namespaces_are_collections() -> None:
    profile = await profile_content(
        XmlParser(),
        b'<r xmlns:a="urn:one"><a:item><a:id>1</a:id></a:item><a:item><a:id>2</a:id></a:item></r>',
    )
    collections = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, TreeObservation)
        and item.observation.role == "record_root"
    ]
    assert any(item.occurrences == 2 for item in collections)
    assert any("urn:one" in step.name for item in collections for step in item.path)


@pytest.mark.anyio
@pytest.mark.integration
async def test_xlsx_merged_ranges_keep_sheet_evidence() -> None:
    from tests.unit.parsers.builtin._document_fixtures import package_parts, zip_bytes

    profile = await profile_content(XlsxParser(), zip_bytes(package_parts("xlsx")))
    merged = [
        item.observation
        for item in profile.observations
        if isinstance(item.observation, TabularObservation)
        and item.observation.role == "merged"
    ]
    assert any(item.merged_ranges == ("A1:B1",) for item in merged)
    assert all(len(item.source_refs) == 2 for item in merged)


@pytest.mark.anyio
async def test_decimal_context_cannot_change_profile_scores() -> None:
    content = b"id,value\n1,12\n2,text\n3,14\n"
    first = await profile_content(DelimitedTextParser(), content)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        second = await profile_content(DelimitedTextParser(), content)
    assert first == second
