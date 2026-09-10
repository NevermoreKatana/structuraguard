"""Недостающие boundaries validator и конечного output stream M05."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from tests.unit.structure.test_execution import execution_context, prepared, stream
from tests.unit.structure.test_profiling import profile_content

from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.exceptions import ParseExecutionError
from structuraguard.parsers.builtin import DelimitedTextParser, PlainTextParser
from structuraguard.structure import ParsePlanExecutor, ParsePlanValidator


@pytest.mark.anyio
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_validator_interruption_closes_source_and_never_accepts(
    interruption: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    entered, closed = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    original_time = loop.time
    shift = 0.0
    monkeypatch.setattr(loop, "time", lambda: original_time() + shift)

    async def blocked() -> AsyncIterator[ExtractedBatch]:
        try:
            yield batches[0]
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(
        ParsePlanValidator(
            options=ParsePlanOptions(
                source_limits=StructuralProfilingOptions(max_processing_seconds=1)
            )
        ).validate_source(request, blocked())
    )
    await entered.wait()
    if interruption == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        shift = 2
        # Готовое событие будит loop; реальное время не участвует в oracle.
        loop.call_soon(lambda: None)
        result = await task
        assert result.validated_plan is None
        assert result.issues[0].code == "PROCESSING_TIMEOUT"
    assert closed.is_set()


@pytest.mark.anyio
@pytest.mark.parametrize("budget", [2, 3])
async def test_output_batch_budget_includes_terminal_manifest(budget: int) -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    options = ParsePlanOptions(max_output_batches=budget)
    validated = (
        ParsePlanValidator(options=options)
        .validate(request, batches=batches)
        .validated_plan
    )
    assert validated is not None
    output = []
    iterator = ParsePlanExecutor(options=options).execute(
        stream(batches), validated, execution_context(request)
    )
    if budget == 3:
        output = [batch async for batch in iterator]
        assert len(output) == 3 and output[-1].is_last
    else:
        with pytest.raises(ParseExecutionError) as failure:
            async for batch in iterator:
                output.append(batch)
        assert failure.value.issue.code == "SECURITY_LIMIT_EXCEEDED"
        assert failure.value.issue.reason == "output_batch_limit"
        assert failure.value.issue.emitted_batches == 2
        assert len(output) == 2 and not any(b.is_last for b in output)


@pytest.mark.anyio
async def test_changed_policy_invalidates_a_serialized_acceptance() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None
    with pytest.raises(ParseExecutionError) as failure:
        await anext(
            ParsePlanExecutor(options=ParsePlanOptions(max_records=20)).execute(
                stream(batches),
                validated,
                execution_context(request),
            )
        )
    assert failure.value.issue.code == "PARSE_PLAN_INVALID"
    assert failure.value.issue.emitted_batches == 0


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["xpath", "css", "eval"])
async def test_expression_operators_are_not_part_of_plan_grammar(
    operation: str,
) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    payload = request.model_dump(mode="python")
    payload["plan"]["fields"][0]["selector"] = {
        "kind": operation,
        "expression": "secret-canary //*[contains(., 'x')]",
    }
    result = ParsePlanValidator().validate(payload, batches=batches)
    assert result.validated_plan is None
    assert result.issues[0].code == "PARSE_PLAN_INVALID"
    assert "secret-canary" not in result.model_dump_json()


@pytest.mark.anyio
async def test_pattern_overflow_is_visible_and_does_not_expand_profile_unboundedly() -> (
    None
):
    options = StructuralProfilingOptions(
        max_patterns=2, max_observations=8, max_candidates=4
    )
    content = "".join(
        f"action{chr(97 + i)} completed\n" * 2 for i in range(20)
    ).encode()
    profile = await profile_content(PlainTextParser(), content, options=options)
    assert profile.coverage is not None
    assert "pattern_limit" in profile.coverage.reasons
    assert not profile.coverage.complete
    assert len(profile.observations) <= options.max_observations
    assert len(profile.candidates) <= options.max_candidates


@pytest.mark.anyio
async def test_oversized_real_value_is_excluded_instead_of_becoming_truncated_evidence() -> (
    None
):
    options = StructuralProfilingOptions(max_value_chars=8)
    profile = await profile_content(
        PlainTextParser(), b"secret-canary-payload\n" * 2, options=options
    )
    assert profile.coverage is not None
    assert profile.coverage.seen_items == 2
    assert profile.coverage.sampled_items == 0
    assert profile.coverage.skipped_items == 2
    assert "oversized_sample" in profile.coverage.reasons
    assert profile.candidates == ()
    assert "secret-canary" not in profile.model_dump_json()
