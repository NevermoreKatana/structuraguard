"""Независимые oracles для duplicate keys, точных budgets и hash seed."""

import asyncio
import json
import os
import subprocess
import sys

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.structure.test_execution import (
    execute,
    execution_context,
    prepared,
    stream,
)

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.parsing import TreePathSelector
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.parsers.builtin import DelimitedTextParser, JsonDocumentParser
from structuraguard.structure import ParsePlanExecutor, ParsePlanValidator


@settings(max_examples=12, deadline=None)
@given(
    key=st.text(alphabet="ab /~[]ёж", min_size=1, max_size=10), count=st.integers(2, 6)
)
def test_duplicate_unicode_keys_keep_both_values_and_physical_occurrences(
    key: str, count: int
) -> None:
    literal = json.dumps(key, ensure_ascii=False)
    content = (
        "["
        + ",".join(
            f'{{{literal}:"left{i}",{literal}:"right{i}"}}' for i in range(count)
        )
        + "]"
    ).encode()

    async def check() -> None:
        for batch_size in (1, 3):
            request, batches = await prepared(
                JsonDocumentParser(), content, batch_size=batch_size
            )
            output = await execute(request, batches)
            records = [r for batch in output for r in batch.records]
            assert len(records) == count
            for i, record in enumerate(records):
                values = [v for e in record.entities for v in e.values]
                by_occurrence = {}
                for field in request.plan.fields:
                    assert isinstance(field.selector, TreePathSelector)
                    assert len(field.selector.steps) == 1
                    assert field.selector.steps[0].name == key
                    value = next(
                        v for v in values if v.field_name == field.semantic_name
                    )
                    by_occurrence[field.selector.steps[0].occurrence] = (
                        value.raw_value.value
                    )
                assert by_occurrence == {0: f"left{i}", 1: f"right{i}"}
                assert len({v.origins[0].source_ref for v in values}) == 2
                assert all(v.raw_value == v.origins[0].raw_value for v in values)

    asyncio.run(check())


@settings(max_examples=10, deadline=None)
@given(count=st.integers(2, 10))
def test_source_item_budget_accepts_exact_n_and_rejects_n_plus_one(count: int) -> None:
    content = ("name,n\n" + "".join(f"r{i},{i}\n" for i in range(count))).encode()

    async def check() -> None:
        request, batches = await prepared(DelimitedTextParser(), content, batch_size=1)
        exact = ParsePlanOptions(
            source_limits=StructuralProfilingOptions(max_total_items=count + 1)
        )
        validator = ParsePlanValidator(options=exact)
        result = await validator.validate_source(request, stream(batches))
        assert result.validated_plan is not None
        output = [
            b
            async for b in ParsePlanExecutor(options=exact).execute(
                stream(batches), result.validated_plan, execution_context(request)
            )
        ]
        assert sum(len(b.records) for b in output) == count
        assert output[-1].is_last
        smaller = ParsePlanOptions(
            source_limits=StructuralProfilingOptions(max_total_items=count)
        )
        rejection = await ParsePlanValidator(options=smaller).validate_source(
            request, stream(batches)
        )
        assert rejection.decision is ValidationDecision.REJECTED
        assert rejection.validated_plan is None
        assert rejection.issues[0].code == "SECURITY_LIMIT_EXCEEDED"

    asyncio.run(check())


def test_profile_ranked_candidates_and_execution_are_hash_seed_independent() -> None:
    outputs = []
    for seed in ("1", "137", "99991"):
        result = subprocess.run(
            [sys.executable, "-m", "tests.property.structure._hashseed_probe"],
            env={
                **os.environ,
                "PYTHONHASHSEED": seed,
                "PYTHONPATH": os.pathsep.join(sys.path),
            },
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        outputs.append(result.stdout)
    assert outputs[0] and len(set(outputs)) == 1
