"""E2E document extraction: chunks, deterministic values и physical provenance."""

import pytest
from tests.fakes.documents import document_script, physical
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context
from tests.unit.parsers.builtin._document_fixtures import pdf_bytes
from tests.unit.parsers.builtin.test_m04_review_regressions import _docx
from tests.unit.structure.test_execution import stream

from structuraguard.contracts import PipelineStatus
from structuraguard.llm import FakeLLMProvider
from structuraguard.parsers.builtin import (
    DocxParser,
    HtmlParser,
    MarkdownParser,
    PdfParser,
    XmlParser,
)
from structuraguard.parsing import ParsingPolicy, SemanticParsingSession
from structuraguard.ports.parser import Parser


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content",
    [
        (MarkdownParser(), b"Supplier Ada agrees with Bob for 1200 on 2026-09-10.\n"),
        (
            HtmlParser(),
            b"<article><p>Supplier Ada agrees with Bob for 1200 on 2026-09-10.</p></article>",
        ),
        (
            XmlParser(),
            b"<contract><supplier>Ada</supplier><customer>Bob</customer><terms><fee>1200</fee><date>2026-09-10</date></terms></contract>",
        ),
        pytest.param(
            PdfParser(),
            pdf_bytes(text="Ada and Bob agree for 1200 on 2026-09-10."),
            marks=pytest.mark.integration,
        ),
        pytest.param(
            DocxParser(),
            _docx(
                "<w:p><w:r><w:t>Ada and Bob agree for 1200 on 2026-09-10.</w:t></w:r></w:p>"
            ),
            marks=pytest.mark.integration,
        ),
    ],
    ids=["prose", "html", "nested-xml", "pdf-contract", "docx-contract"],
)
async def test_document_exact_values_have_physical_spans(
    parser: Parser, content: bytes
) -> None:
    batches = await physical(parser, content)
    policy = ParsingPolicy()
    provider = FakeLLMProvider(
        await document_script(batches, policy), clock=fixed_clock
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        policy=policy,
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        assert session.report is not None
        assert session.report.status is PipelineStatus.COMPLETED, session.report.issues
        assert output[-1].is_last
        values = [
            value
            for batch in output
            for record in batch.records
            for entity in record.entities
            for value in entity.values
        ]
        assert {value.normalized_value.value for value in values} >= {
            "Ada",
            "Bob",
            "1200",
            "2026-09-10",
        }
        manifest = batches[-1].manifest
        assert manifest is not None
        for value in values:
            assert value.source_refs and set(value.source_refs) <= set(
                manifest.source_index.refs
            )
            assert all(origin.source_spans for origin in value.origins)
        assert session.report.provenance_coverage == 1
        assert session.report.llm_calls == provider.call_count == 1
        assert session.report.input_tokens == 20 and session.report.output_tokens == 30
        assert (
            session.report.plan is not None and session.report.plan.semantic_generations
        )


@pytest.mark.anyio
async def test_xml_parent_child_entities_keep_xpath_and_stable_record() -> None:
    from tests.fakes.documents import chunks_for, suggestion

    from structuraguard.contracts import XPathLocation
    from structuraguard.llm import ScriptedResponse

    batches = await physical(
        XmlParser(),
        b"<contract><supplier>Ada</supplier><terms><fee>1200</fee></terms></contract>",
    )
    policy = ParsingPolicy()
    proposed = suggestion((await chunks_for(batches, policy))[0])
    data = proposed.model_dump()
    data["entities"][0]["entity_type"] = "contract"
    data["entities"][1]["entity_type"] = "term"
    data["entities"][1]["parent_entity_id"] = data["entities"][0]["entity_id"]
    provider = FakeLLMProvider(
        (
            ScriptedResponse(
                output_json=type(proposed).model_validate(data).canonical_json()
            ),
        ),
        clock=fixed_clock,
    )
    async with SemanticParsingSession(
        replay=lambda: stream(batches),
        provider=provider,
        scanner=Scanner(),
        context=context(),
        clock=fixed_clock,
        timer=lambda: 0,
    ) as session:
        output = [batch async for batch in session.parse_semantically()]
        records = [r for b in output for r in b.records]
        assert len(records) == 1 and len(records[0].entities) == 2
        parent, child = records[0].entities
        assert child.parent_entity_id == parent.entity_id
        assert all(
            isinstance(origin.location, XPathLocation)
            for e in records[0].entities
            for value in e.values
            for origin in value.origins
        )
        assert (
            session.report is not None
            and session.report.status is PipelineStatus.COMPLETED
        )
