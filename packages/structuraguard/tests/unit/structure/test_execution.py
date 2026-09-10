"""Сквозной parser → analyzer → validator → streaming executor M5-C."""

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlanValidationRequest,
    StructurePlanCreated,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
)
from structuraguard.ports.parser import Parser
from structuraguard.structure import (
    DeterministicStructureAnalyzer,
    ParsePlanExecutor,
    ParsePlanValidator,
)


async def stream(batches: tuple[ExtractedBatch, ...]) -> AsyncIterator[ExtractedBatch]:
    for batch in batches:
        yield batch


async def prepared(
    parser: Parser, content: bytes, *, batch_size: int = 2
) -> tuple[ParsePlanValidationRequest, tuple[ExtractedBatch, ...]]:
    source = source_for(content, display_name="source")
    context = contexts_for(source, content, batch_size=batch_size)[1]
    batches = await collect(parser, source, context)
    analysis = await DeterministicStructureAnalyzer().analyze(stream(batches))
    assert isinstance(analysis, StructurePlanCreated)
    manifest = batches[-1].manifest
    assert manifest is not None
    return ParsePlanValidationRequest(
        plan=analysis.plan,
        source=manifest.source,
        manifest=manifest,
        profile=analysis.profile,
    ), batches


def execution_context(
    request: ParsePlanValidationRequest, *, batch_size: int = 1
) -> ParseExecutionContext:
    return ParseExecutionContext(
        run_id="execution_test",
        source_fingerprint=request.source.source_fingerprint,
        extraction_fingerprint=request.manifest.extraction_fingerprint,
        parse_plan_fingerprint=request.plan.fingerprint,
        manifest=request.manifest,
        profile=request.profile,
        max_records_per_batch=batch_size,
    )


async def execute(
    request: ParsePlanValidationRequest, batches: tuple[ExtractedBatch, ...]
) -> tuple[NormalizedBatch, ...]:
    result = await ParsePlanValidator().validate_source(request, stream(batches))
    assert result.decision is ValidationDecision.ACCEPTED
    assert result.validated_plan is not None
    output = tuple(
        [
            batch
            async for batch in ParsePlanExecutor().execute(
                stream(batches), result.validated_plan, execution_context(request)
            )
        ]
    )
    assert output[-1].is_last
    assert output[-1].manifest is not None
    output[-1].manifest.validate_batches(output)
    return output


@pytest.mark.anyio
async def test_tabular_ranges_raw_values_and_physical_provenance() -> None:
    request, batches = await prepared(
        DelimitedTextParser(),
        b"Report\nname,count,code\nAda,1,001\nBob,2,002\nname,count,code\nCara,3,003\nTotal,6,\n",
    )
    output = await execute(request, batches)
    records = [r for batch in output for r in batch.records]
    assert len(records) == 3
    assert [
        v.raw_value.value for r in records for e in r.entities for v in e.values
    ] == ["Ada", "1", "001", "Bob", "2", "002", "Cara", "3", "003"]
    known_refs = {ref for batch in batches for ref in batch.physical_refs()}
    for record in records:
        assert set(record.source_refs) <= known_refs
        for entity in record.entities:
            for value in entity.values:
                assert value.origins
                assert value.raw_value == value.normalized_value
                assert all(
                    origin.location.source == request.source for origin in value.origins
                )


@pytest.mark.anyio
async def test_nested_tree_records_keep_parent_children_without_cartesian_product() -> (
    None
):
    request, batches = await prepared(
        JsonDocumentParser(),
        b'[{"id":1,"items":[{"sku":"a"},{"sku":"b"}]},{"id":2,"items":[{"sku":"c"}]}]',
        batch_size=1,
    )
    output = await execute(request, batches)
    records = [r for batch in output for r in batch.records]
    assert [len(r.entities) for r in records] == [3, 2]
    for record in records:
        root = record.entities[0]
        assert root.parent_entity_id is None
        assert all(e.parent_entity_id == root.entity_id for e in record.entities[1:])
    values = [v.raw_value.value for r in records for e in r.entities for v in e.values]
    assert values == ["1", "a", "b", "2", "c"]


@pytest.mark.anyio
async def test_log_groups_cross_batches_and_preserve_each_raw_line() -> None:
    request, batches = await prepared(
        PlainTextParser(),
        b"metric cpu 1\n  detail one\nmetric cpu 2\n  detail two\n",
        batch_size=1,
    )
    output = await execute(request, batches)
    values = [
        v
        for batch in output
        for r in batch.records
        for e in r.entities
        for v in e.values
    ]
    assert [tuple(o.raw_value.value for o in v.origins) for v in values] == [
        ("metric cpu 1", "  detail one"),
        ("metric cpu 2", "  detail two"),
    ]
    assert all(v.transformations == ("join_lines",) for v in values)


@pytest.mark.anyio
async def test_validator_requires_source_and_checks_serialized_request() -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,count\nAda,1\nBob,2\n"
    )
    validator = ParsePlanValidator()
    missing = validator.validate(request)
    assert missing.decision is ValidationDecision.REJECTED
    assert missing.issues[0].code == "PARSE_PLAN_REPLAY_REQUIRED"
    validated = validator.validate(request.model_dump(mode="python"), batches=batches)
    assert validated.decision is ValidationDecision.ACCEPTED


@pytest.mark.anyio
@pytest.mark.integration
async def test_document_blocks_keep_page_locations() -> None:
    from tests.unit.parsers.builtin._document_fixtures import pdf_bytes

    from structuraguard.parsers.builtin import PdfParser

    request, batches = await prepared(PdfParser(), pdf_bytes(pages=2), batch_size=1)
    output = await execute(request, batches)
    origins = [
        o
        for batch in output
        for r in batch.records
        for e in r.entities
        for v in e.values
        for o in v.origins
    ]
    assert len(origins) == 2
    assert all(o.location.kind == "document_block" for o in origins)
    assert origins[0].location != origins[1].location


@pytest.mark.anyio
@pytest.mark.integration
async def test_docx_text_blocks_execute_without_losing_raw_text() -> None:
    from tests.unit.parsers.builtin._document_fixtures import package_parts, zip_bytes

    from structuraguard.parsers.builtin import DocxParser

    parts = package_parts("docx")
    parts["word/document.xml"] = (
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Name: Ada</w:t></w:r></w:p><w:p><w:r><w:t>Name: Bob</w:t></w:r></w:p></w:body></w:document>'
    )
    request, batches = await prepared(DocxParser(), zip_bytes(parts), batch_size=1)
    output = await execute(request, batches)
    assert [
        v.raw_value.value
        for b in output
        for r in b.records
        for e in r.entities
        for v in e.values
    ] == ["Name: Ada", "Name: Bob"]


@pytest.mark.anyio
@pytest.mark.parametrize("missing_descendant", [False, True])
async def test_empty_array_scope_requires_a_physically_verifiable_path(
    missing_descendant: bool,
) -> None:
    from structuraguard.contracts.analysis import TreePathOperation, TreeStep
    from structuraguard.contracts.common import PhysicalObjectKind
    from structuraguard.contracts.parsing import (
        ParseEntity,
        ParseField,
        TreeNodeGrouping,
        TreeParsePlan,
        TreePathSelector,
    )
    from structuraguard.structure import StructuralProfiler

    content = b"[]"
    source = source_for(content, display_name="empty")
    batches = await collect(
        JsonDocumentParser(), source, contexts_for(source, content)[1]
    )
    profile = await StructuralProfiler().profile(stream(batches))
    manifest = batches[-1].manifest
    assert manifest is not None
    root = next(
        ref
        for ref in manifest.source_index.refs
        if ref.kind is PhysicalObjectKind.TREE_NODE
    )
    steps: tuple[TreeStep, ...] = (TreeStep(operation=TreePathOperation.ITEM),)
    if missing_descendant:
        steps += (TreeStep(operation=TreePathOperation.KEY, name="missing"),)
    plan = TreeParsePlan(
        plan_id="empty_tree",
        schema_version="1.1.0",
        revision=1,
        source_fingerprint=manifest.source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        profile_fingerprint=profile.profile_fingerprint,
        confidence=Decimal(1),
        producer=profile.producer,
        root_ref=root,
        record_steps=steps,
        fields=(
            ParseField(
                field_id="value",
                semantic_name="value",
                semantic_type="unresolved",
                source_refs=(root,),
                selector=TreePathSelector(),
            ),
        ),
        entities=(
            ParseEntity(
                entity_id="items",
                entity_type="unresolved",
                field_ids=("value",),
                grouping=TreeNodeGrouping(record_steps=steps),
            ),
        ),
        evidence=(root,),
    )
    request = ParsePlanValidationRequest(
        plan=plan, source=manifest.source, manifest=manifest, profile=profile
    )
    if missing_descendant:
        result = ParsePlanValidator().validate(request, batches=batches)
        assert result.validated_plan is None
        assert result.issues[0].message_key == "RECORD_PATH_MISSING_OR_INCOMPLETE"
        return
    output = await execute(request, batches)
    assert len(output) == 1 and output[0].is_last and not output[0].records


@pytest.mark.anyio
async def test_explicit_large_scope_streams_beyond_profile_samples_with_bounded_state() -> (
    None
):
    from structuraguard.contracts.common import PhysicalObjectKind
    from structuraguard.contracts.execution import ParsePlanOptions
    from structuraguard.contracts.parsing import (
        ParseEntity,
        ParseField,
        TabularColumnSelector,
        TabularParsePlan,
        TabularRowGrouping,
    )
    from structuraguard.structure import StructuralProfiler
    from structuraguard.structure._runtime import PlanRuntime

    content = ("name,n\n" + "".join(f"r{i},{i}\n" for i in range(300))).encode()
    source = source_for(content, display_name="large")
    batches = await collect(
        DelimitedTextParser(), source, contexts_for(source, content, batch_size=10)[1]
    )
    profile = await StructuralProfiler().profile(stream(batches))
    assert profile.coverage is not None and not profile.coverage.complete
    manifest = batches[-1].manifest
    assert manifest is not None
    table = next(
        r for r in manifest.source_index.refs if r.kind is PhysicalObjectKind.TABLE
    )
    fields = tuple(
        ParseField(
            field_id=f"column_{i}",
            semantic_name=f"column_{i}",
            semantic_type="unresolved",
            source_refs=(table,),
            selector=TabularColumnSelector(column_index=i),
        )
        for i in range(2)
    )
    plan = TabularParsePlan(
        plan_id="explicit_large",
        revision=1,
        source_fingerprint=manifest.source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        profile_fingerprint=profile.profile_fingerprint,
        producer=profile.producer,
        confidence=Decimal(1),
        fields=fields,
        entities=(
            ParseEntity(
                entity_id="rows",
                entity_type="unresolved",
                field_ids=tuple(f.field_id for f in fields),
                grouping=TabularRowGrouping(),
            ),
        ),
        evidence=(table,),
        table_ref=table,
        header_row=0,
        data_start_row=1,
        data_end_row=300,
    )
    request = ParsePlanValidationRequest(
        plan=plan, source=manifest.source, manifest=manifest, profile=profile
    )
    runtime = PlanRuntime(request, ParsePlanOptions())
    count = 0
    for batch in batches:
        count += sum(1 for _ in runtime.consume(batch))
        assert all(len(refs) <= 1 for refs in runtime.field_seen.values())
    assert not list(runtime.finish())
    assert count == 300
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None
    consumed = 0

    async def source_stream() -> AsyncIterator[ExtractedBatch]:
        nonlocal consumed
        for batch in batches:
            consumed += 1
            yield batch

    iterator = ParsePlanExecutor().execute(
        source_stream(), validated, execution_context(request, batch_size=7)
    )
    first = await anext(iterator)
    assert len(first.records) == 7 and consumed < len(batches)
    total = len(first.records)
    async for normalized_batch in iterator:
        total += len(normalized_batch.records)
    assert total == 300
