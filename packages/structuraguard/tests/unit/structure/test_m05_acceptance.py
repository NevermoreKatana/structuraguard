"""Недостающее покрытие форматов и неоднозначности из критериев M05."""

import pytest
from tests.unit.structure.test_analysis import analyze_content
from tests.unit.structure.test_execution import execute, prepared
from tests.unit.structure.test_profiling import profile_content

from structuraguard.contracts.parsing import StructureNeedsReview
from structuraguard.contracts.structure import FieldObservation
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    HtmlParser,
    JsonLinesParser,
    MarkdownParser,
    XmlParser,
    YamlParser,
)
from structuraguard.ports.parser import Parser


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "expected"),
    [
        (
            DelimitedTextParser(),
            b"name\tcount\nAda\t1\nBob\t2\n",
            ["Ada", "1", "Bob", "2"],
        ),
        (YamlParser(), b"- name: Ada\n- name: Bob\n", ["name", "Ada", "name", "Bob"]),
    ],
    ids=["tsv", "yaml"],
)
async def test_remaining_formats_preserve_values_through_m05(
    parser: Parser,
    content: bytes,
    expected: list[str],
) -> None:
    request, batches = await prepared(parser, content, batch_size=1)
    output = await execute(request, batches)
    assert [
        v.raw_value.value
        for batch in output
        for record in batch.records
        for entity in record.entities
        for v in entity.values
    ] == expected
    physical = {ref for batch in batches for ref in batch.physical_refs()}
    assert all(
        origin.source_ref in physical and origin.location.source == request.source
        for batch in output
        for record in batch.records
        for entity in record.entities
        for value in entity.values
        for origin in value.origins
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("parser", "content", "issue_code"),
    [
        (
            JsonLinesParser(),
            b'{"name":"Ada"}\n{"name":"Bob"}\n',
            "STRUCTURE_RULES_INSUFFICIENT",
        ),
        (
            XmlParser(),
            b"<root><item><name>Ada</name></item><item><name>Bob</name></item></root>",
            "STRUCTURE_RULES_INSUFFICIENT",
        ),
        (
            HtmlParser(),
            b"<p>Name: Ada</p><p>Name: Bob</p>",
            "STRUCTURE_UNHANDLED_SCOPE",
        ),
        (MarkdownParser(), b"Name: Ada\n\nName: Bob\n", "STRUCTURE_RULES_INSUFFICIENT"),
    ],
    ids=[
        "jsonl_multiple_roots",
        "xml_element_policy",
        "html_parallel_tree",
        "markdown_blank_lines",
    ],
)
async def test_unsupported_automatic_scope_requires_semantics_without_losing_data(
    parser: Parser,
    content: bytes,
    issue_code: str,
) -> None:
    result = await analyze_content(parser, content)
    assert result.kind == "needs_semantic_analysis"
    assert result.code == "NEEDS_SEMANTIC_ANALYSIS"
    assert result.issues[0].code == issue_code


@pytest.mark.anyio
async def test_document_with_table_and_sections_keeps_competing_candidates() -> None:
    content = b"<h1>Entry</h1><p>Name: Ada</p><h1>Entry</h1><p>Name: Bob</p><table><tr><th>name</th><th>count</th></tr><tr><td>pen</td><td>2</td></tr><tr><td>book</td><td>3</td></tr></table>"
    first = await analyze_content(HtmlParser(), content)
    second = await analyze_content(HtmlParser(), content)
    assert isinstance(first, StructureNeedsReview)
    assert first == second
    assert len(first.candidates) > 1
    assert {"document", "tabular"} <= {c.plan_kind.value for c in first.candidates}
    assert all(c.assessment is not None and c.observation_ids for c in first.candidates)


@pytest.mark.anyio
async def test_mixed_primitive_hints_preserve_all_observed_alternatives() -> None:
    profile = await profile_content(
        DelimitedTextParser(), b"id,value\n1,12\n2,text\n3,14\n"
    )
    evidence = next(
        item
        for item in profile.observations
        if isinstance(item.observation, FieldObservation)
        and item.observation.column_index == 1
    )
    field = evidence.observation
    assert isinstance(field, FieldObservation)
    assert field.type_counts["integer"] == 2
    assert field.type_counts["string"] >= 1
    assert sum(field.type_counts.values()) == field.sampled_count
    assert evidence.source_refs and 0 < evidence.confidence < 1
