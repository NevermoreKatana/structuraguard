"""Пустые строки вне выбранного scope не раздувают индекс sparse CSV."""

from collections import defaultdict
from dataclasses import replace

import pytest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import execution_context, prepared, stream

from structuraguard.contracts.common import PhysicalObjectKind, ValidationDecision
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.parsing import (
    ParsePlanValidationRequest,
    TabularParsePlan,
)
from structuraguard.contracts.source import ExtractedCell
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.structure import (
    ParsePlanExecutor,
    ParsePlanValidator,
    StructuralProfiler,
    _runtime,
)


@pytest.mark.anyio
@pytest.mark.parametrize("component", ["validator", "executor"])
async def test_sparse_rows_do_not_accumulate_outside_selected_scope(
    component: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = b"name,n\nAda,1\nBob,2\n"
    template, _ = await prepared(DelimitedTextParser(), prefix)
    content = prefix + b"\n" * 4_997
    source = source_for(content, display_name="sparse.csv")
    context = replace(
        contexts_for(source, content, batch_size=6_000)[1], max_records=6_000
    )
    batches = await collect(DelimitedTextParser(), source, context)
    manifest = batches[-1].manifest
    assert manifest is not None
    assert len(batches) == 1
    assert len(batches[0].tables[0].cells) == 6
    profile = await StructuralProfiler(
        options=StructuralProfilingOptions(max_sample_items=8, tail_rows=0)
    ).profile(stream(batches))
    table_ref = next(
        ref
        for ref in manifest.source_index.refs
        if ref.kind is PhysicalObjectKind.TABLE
    )
    payload = template.plan.model_dump(mode="python")
    payload.update(
        fingerprint="sha256:" + "0" * 64,
        analysis=None,
        source_fingerprint=source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        profile_fingerprint=profile.profile_fingerprint,
        table_ref=table_ref,
        evidence=(table_ref,),
    )
    payload["fields"] = tuple(
        {**field, "source_refs": (table_ref,)} for field in payload["fields"]
    )
    request = ParsePlanValidationRequest(
        source=manifest.source,
        manifest=manifest,
        profile=profile,
        plan=TabularParsePlan.model_validate(payload),
    )
    options = ParsePlanOptions(
        source_limits=StructuralProfilingOptions(max_batch_bytes=65_536),
        max_record_items=10,
    )
    baseline = await ParsePlanValidator(options=options).validate_source(
        request, stream(batches)
    )
    assert baseline.validated_plan is not None
    index_sizes: list[int] = []

    class ObservedRows(defaultdict[int, dict[int, ExtractedCell]]):
        def __missing__(self, key: int) -> dict[int, ExtractedCell]:
            value = super().__missing__(key)
            index_sizes.append(len(self))
            return value

    # Инструментация сохраняет поведение defaultdict и измеряет реальные
    # allocations индекса; fixture ограничен 5 000 строками даже до исправления.
    monkeypatch.setattr(_runtime, "defaultdict", ObservedRows)
    if component == "validator":
        result = await ParsePlanValidator(options=options).validate_source(
            request, stream(batches)
        )
        assert result.decision is ValidationDecision.ACCEPTED
    else:
        output = tuple(
            [
                batch
                async for batch in ParsePlanExecutor(options=options).execute(
                    stream(batches), baseline.validated_plan, execution_context(request)
                )
            ]
        )
        assert output[-1].is_last
        assert output[-1].manifest is not None
        output[-1].manifest.validate_batches(output)
        assert [
            value.raw_value.value
            for batch in output
            for record in batch.records
            for entity in record.entities
            for value in entity.values
        ] == ["Ada", "1", "Bob", "2"]
    assert index_sizes
    assert max(index_sizes) <= 3
