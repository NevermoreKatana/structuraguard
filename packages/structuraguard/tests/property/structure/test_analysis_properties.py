"""Независимые invariants координат, повторяемости и lossless field выбора."""

import asyncio
import json
from decimal import ROUND_UP, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.security.structure.test_profile_security import stream
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_analysis import analyze_content

from structuraguard.contracts.analysis import (
    ExplicitRecordGrouping,
    TreePathOperation,
    TreeStep,
)
from structuraguard.contracts.parsing import (
    LogParsePlan,
    StructurePlanCreated,
    TabularColumnSelector,
    TabularParsePlan,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
)
from structuraguard.structure import DeterministicStructureAnalyzer


@settings(max_examples=15, deadline=None)
@given(values=st.lists(st.integers(-999, 999), min_size=2, max_size=20))
def test_csv_row_ranges_and_columns_exist_and_repeat(values: list[int]) -> None:
    content = ("label,number\n" + "".join(f"row,{n}\n" for n in values)).encode()

    async def check() -> None:
        source = source_for(content, display_name="table")
        batches = await collect(
            DelimitedTextParser(),
            source,
            contexts_for(source, content, batch_size=3)[1],
        )
        analyzer = DeterministicStructureAnalyzer()
        first = await analyzer.analyze(stream(batches))
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_UP
            second = await analyzer.analyze(stream(batches))
        assert first == second
        assert isinstance(first, StructurePlanCreated)
        plan = first.plan
        assert isinstance(plan, TabularParsePlan)
        cells = {
            (c.row_index, c.column_index)
            for batch in batches
            for table in batch.tables
            for c in table.cells
        }
        assert plan.data_end_row == len(values)
        for row in range(plan.data_start_row, plan.data_end_row + 1):
            for field in plan.fields:
                assert isinstance(field.selector, TabularColumnSelector)
                assert (row, field.selector.column_index) in cells

    asyncio.run(check())


@settings(max_examples=15, deadline=None)
@given(count=st.integers(2, 20), continuation_count=st.integers(0, 3))
def test_log_explicit_groups_cover_every_physical_line_once(
    count: int, continuation_count: int
) -> None:
    content = (("metric cpu 1\n" + "  detail\n" * continuation_count) * count).encode()

    async def check() -> None:
        result = await analyze_content(PlainTextParser(), content)
        assert result.profile.coverage is not None
        if not result.profile.coverage.complete:
            assert result.kind == "needs_semantic_analysis"
            assert "candidate_limit" in result.profile.coverage.reasons
            return
        assert isinstance(result, StructurePlanCreated)
        plan = result.plan
        assert isinstance(plan, LogParsePlan)
        grouping = plan.entities[0].grouping
        assert isinstance(grouping, ExplicitRecordGrouping)
        assert len(grouping.records) == count
        assert all(len(record) == continuation_count + 1 for record in grouping.records)
        assert (
            tuple(ref for record in grouping.records for ref in record)
            == plan.line_refs
        )
        assert len(set(plan.line_refs)) == count * (continuation_count + 1)

    asyncio.run(check())


@settings(max_examples=15, deadline=None)
@given(
    key=st.text(alphabet="abc_ /[]~ёж", min_size=1, max_size=20),
    count=st.integers(2, 8),
)
def test_literal_tree_steps_resolve_in_the_original_json(key: str, count: int) -> None:
    data = [{key: {"value": n}} for n in range(count)]

    async def check() -> None:
        result = await analyze_content(
            JsonDocumentParser(), json.dumps(data, ensure_ascii=False).encode()
        )
        assert isinstance(result, StructurePlanCreated)
        assert isinstance(result.plan, TreeParsePlan)
        assert result.plan.record_steps == (TreeStep(operation=TreePathOperation.ITEM),)
        for field in result.plan.fields:
            assert isinstance(field.selector, TreePathSelector)
            assert tuple(step.name for step in field.selector.steps) == (key, "value")
            assert all(
                step.operation is TreePathOperation.KEY for step in field.selector.steps
            )
            for record in data:
                assert record[field.selector.steps[0].name][
                    field.selector.steps[1].name
                ] in range(count)

    asyncio.run(check())
