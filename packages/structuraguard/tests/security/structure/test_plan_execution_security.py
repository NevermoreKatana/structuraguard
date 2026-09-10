"""Malicious plans, forged wrappers, partial failure и bounded execution."""

import asyncio
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Iterator

import pytest
from tests.unit.structure.test_execution import (
    execute,
    execution_context,
    prepared,
    stream,
)

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.parsing import (
    ParsePlanValidationRequest,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import ParseExecutionError
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
)
from structuraguard.structure import ParsePlanExecutor, ParsePlanValidator
from structuraguard.structure._plan_check import validation_fingerprint


@pytest.mark.anyio
async def test_source_iterator_creation_errors_are_sanitized() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    validator = ParsePlanValidator()
    validated = validator.validate(request, batches=batches).validated_plan
    assert validated is not None

    class BrokenSource:
        def __iter__(self) -> Iterator[ExtractedBatch]:
            raise KeyError("secret-canary")

        def __aiter__(self) -> AsyncIterator[ExtractedBatch]:
            raise KeyError("secret-canary")

    for result in (
        validator.validate(request, batches=BrokenSource()),
        await validator.validate_source(request, BrokenSource()),
    ):
        assert result.validated_plan is None
        assert result.issues[0].code == "PARSE_EXECUTION_SOURCE_ERROR"
        assert "secret-canary" not in result.model_dump_json()
    with pytest.raises(ParseExecutionError) as failure:
        await anext(
            ParsePlanExecutor().execute(
                BrokenSource(), validated, execution_context(request)
            )
        )
    assert failure.value.issue.reason == "source_open_failed"
    assert failure.value.issue.emitted_batches == 0
    assert "secret-canary" not in str(failure.value)


@pytest.mark.anyio
@pytest.mark.parametrize("operator", ["sql", "python", "shell", "callback", "regex"])
async def test_malicious_plan_payload_is_rejected_without_source_code_execution(
    operator: str,
) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    payload = request.model_dump(mode="python")
    payload["plan"]["rules"] = [{"kind": operator, "body": "secret-canary (a+)+$"}]
    result = ParsePlanValidator().validate(payload, batches=batches)
    assert result.decision is ValidationDecision.REJECTED
    assert result.validated_plan is None
    assert result.issues[0].code == "PARSE_PLAN_INVALID"
    assert "secret-canary" not in result.model_dump_json()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    ["fingerprint", "schema", "unknown_ref", "row", "column", "overlapping_rule"],
)
async def test_false_tabular_claims_never_receive_validated_plan(mutation: str) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    payload = request.model_dump(mode="python")
    plan = payload["plan"]
    plan["analysis"] = None
    plan["fingerprint"] = "sha256:" + "0" * 64
    if mutation == "fingerprint":
        plan["source_fingerprint"] = "sha256:" + "f" * 64
    elif mutation == "schema":
        plan["schema_version"] = "9.9.9"
    elif mutation == "unknown_ref":
        plan["table_ref"]["local_id"] = "unknown_table"
    elif mutation == "row":
        plan["data_end_row"] = 100
    elif mutation == "column":
        plan["fields"][0]["selector"]["column_index"] = 7
    else:
        plan["rules"] = [
            {"kind": "record_range", "start_index": 1, "end_index": 2},
            {"kind": "record_range", "start_index": 2, "end_index": 3},
        ]
    result = ParsePlanValidator().validate(payload, batches=batches)
    assert result.decision is ValidationDecision.REJECTED
    assert result.validated_plan is None


@pytest.mark.anyio
async def test_tree_path_and_physical_group_order_are_verified() -> None:
    request, batches = await prepared(JsonDocumentParser(), b'[{"id":1},{"id":2}]')
    payload = request.model_dump(mode="python")
    payload["plan"].update(analysis=None, fingerprint="sha256:" + "0" * 64)
    payload["plan"]["fields"][0]["selector"]["steps"][0]["name"] = "missing"
    assert (
        ParsePlanValidator().validate(payload, batches=batches).decision
        is ValidationDecision.REJECTED
    )
    request, batches = await prepared(
        PlainTextParser(),
        b"metric cpu 1\n  detail\nmetric cpu 2\n  detail\n",
        batch_size=1,
    )
    payload = request.model_dump(mode="python")
    payload["plan"].update(analysis=None, fingerprint="sha256:" + "0" * 64)
    records = payload["plan"]["entities"][0]["grouping"]["records"]
    payload["plan"]["entities"][0]["grouping"]["records"] = tuple(
        tuple(reversed(record)) for record in records
    )
    assert (
        ParsePlanValidator().validate(payload, batches=batches).decision
        is ValidationDecision.REJECTED
    )


@pytest.mark.anyio
async def test_forged_checked_wrapper_does_not_authorize_missing_rows() -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None
    payload = request.model_dump(mode="python")
    payload["plan"].update(
        analysis=None, data_end_row=99, fingerprint="sha256:" + "0" * 64
    )
    forged_request = ParsePlanValidationRequest.model_validate(payload)
    wrapper = validated.model_dump(mode="python")
    wrapper.update(
        plan=forged_request.plan,
        plan_fingerprint=forged_request.plan.fingerprint,
        validation_fingerprint=validation_fingerprint(
            forged_request, ParsePlanOptions()
        ),
    )
    forged = ValidatedParsePlan.model_validate(wrapper)
    output = []
    with pytest.raises(ParseExecutionError) as failure:
        async for batch in ParsePlanExecutor().execute(
            stream(batches), forged, execution_context(forged_request)
        ):
            output.append(batch)
    assert failure.value.issue.reason == "row_range_missing"
    assert failure.value.issue.emitted_batches == len(output)
    assert not any(batch.is_last for batch in output)


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [RuntimeError, KeyError])
async def test_late_source_error_has_typed_issue_no_terminal_and_closes_iterator(
    error_type: type[Exception],
) -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\nCara,3\n", batch_size=1
    )
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            for batch in batches[:-1]:
                yield batch
            raise error_type("secret-canary")
        finally:
            closed.set()

    output = []
    with pytest.raises(ParseExecutionError) as failure:
        async for batch in ParsePlanExecutor().execute(
            source(), validated, execution_context(request)
        ):
            output.append(batch)
    assert output and not any(batch.is_last for batch in output)
    assert failure.value.issue.stage == "source"
    assert "secret-canary" not in str(failure.value)
    assert closed.is_set()


@pytest.mark.anyio
async def test_cancellation_and_consumer_close_release_source() -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None
    entered, closed = asyncio.Event(), asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batches[0]
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    iterator = ParsePlanExecutor().execute(
        source(), validated, execution_context(request)
    )
    task = asyncio.ensure_future(anext(iterator))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    closed.clear()

    async def finite() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            for batch in batches:
                yield batch
        finally:
            closed.set()

    iterator = ParsePlanExecutor().execute(
        finite(), validated, execution_context(request)
    )
    assert not (await anext(iterator)).is_last
    await iterator.aclose()
    assert closed.is_set()


@pytest.mark.anyio
async def test_resource_budgets_and_model_construct_fail_closed() -> None:
    result = ParsePlanValidator().validate(ParsePlanValidationRequest.model_construct())
    assert result.decision is ValidationDecision.REJECTED
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    result = ParsePlanValidator(options=ParsePlanOptions(max_plan_bytes=32)).validate(
        request, batches=batches
    )
    assert result.issues[0].code == "SECURITY_LIMIT_EXCEEDED"
    result = ParsePlanValidator(options=ParsePlanOptions(max_record_bytes=32)).validate(
        request, batches=batches
    )
    assert result.issues[0].code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_source_literals_do_not_trigger_code_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    request, batches = await prepared(
        JsonDocumentParser(),
        b'[{"SELECT * FROM t; $(cmd)":1},{"SELECT * FROM t; $(cmd)":2}]',
    )
    output = await execute(request, batches)
    assert [
        v.raw_value.value
        for b in output
        for r in b.records
        for e in r.entities
        for v in e.values
    ] == ["1", "2"]


@pytest.mark.anyio
async def test_executor_deadline_is_typed_and_has_no_timer_during_consumer_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structuraguard.contracts.structure import StructuralProfilingOptions

    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    options = ParsePlanOptions(
        source_limits=StructuralProfilingOptions(max_processing_seconds=1)
    )
    validated = (
        ParsePlanValidator(options=options)
        .validate(request, batches=batches)
        .validated_plan
    )
    assert validated is not None
    iterator = ParsePlanExecutor(options=options).execute(
        stream(batches), validated, execution_context(request)
    )
    assert not (await anext(iterator)).is_last
    loop = asyncio.get_running_loop()
    future = loop.time() + 10
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: future)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    # Время потребителя не входит в active deadline.
    remainder = [batch async for batch in iterator]
    assert remainder[-1].is_last
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batches[0]
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    iterator = ParsePlanExecutor(options=options).execute(
        source(), validated, execution_context(request)
    )
    task = asyncio.ensure_future(anext(iterator))
    await waiting.wait()
    future = loop.time() + 2
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: future)
        with pytest.raises(ParseExecutionError) as failure:
            await task
    assert failure.value.issue.code == "PROCESSING_TIMEOUT"
    assert closed.is_set()


@pytest.mark.anyio
async def test_cleanup_failure_does_not_mask_primary_or_create_terminal() -> None:
    request, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batches[0]
            raise RuntimeError("primary-secret")
        finally:
            raise OSError("cleanup-secret")

    with pytest.raises(ParseExecutionError) as failure:
        async for _ in ParsePlanExecutor().execute(
            source(), validated, execution_context(request)
        ):
            pass
    assert failure.value.issue.code == "PARSE_EXECUTION_SOURCE_ERROR"
    assert "secret" not in str(failure.value)


@pytest.mark.anyio
async def test_normalized_fingerprint_detects_post_execution_payload_changes() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    output = await execute(request, batches)
    first = output[0]
    forged = first.model_copy(update={"records": ()})
    assert output[-1].manifest is not None
    with pytest.raises(ValueError):
        output[-1].manifest.validate_batch(forged)


@pytest.mark.anyio
async def test_cleanup_only_failure_prevents_successful_terminal() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    validated = ParsePlanValidator().validate(request, batches=batches).validated_plan
    assert validated is not None

    class FailingClose:
        def __init__(self) -> None:
            self.position = 0

        def __aiter__(self) -> "FailingClose":
            return self

        async def __anext__(self) -> ExtractedBatch:
            if self.position == len(batches):
                raise StopAsyncIteration
            batch = batches[self.position]
            self.position += 1
            return batch

        async def aclose(self) -> None:
            raise OSError("cleanup-secret")

    source = FailingClose()
    output = []
    with pytest.raises(ParseExecutionError) as failure:
        async for batch in ParsePlanExecutor().execute(
            source, validated, execution_context(request)
        ):
            output.append(batch)
    assert failure.value.issue.stage == "cleanup"
    assert not any(b.is_last for b in output)
    rejected = await ParsePlanValidator().validate_source(request, FailingClose())
    assert rejected.decision is ValidationDecision.REJECTED


@pytest.mark.anyio
async def test_token_work_is_bounded_before_full_tokenization() -> None:
    request, batches = await prepared(
        PlainTextParser(), b"metric cpu user=1 done\nmetric cpu user=2 done\n"
    )
    payload = request.model_dump(mode="python")
    payload["plan"].update(analysis=None, fingerprint="sha256:" + "0" * 64)
    payload["plan"]["fields"][0]["selector"] = {
        "kind": "log_token",
        "line_offset": 0,
        "token_index": 1,
        "delimiter": "whitespace",
    }
    result = ParsePlanValidator(options=ParsePlanOptions(max_record_items=3)).validate(
        payload, batches=batches
    )
    assert result.decision is ValidationDecision.REJECTED
    assert result.issues[0].message_key == "TOKEN_LIMIT"


@pytest.mark.anyio
async def test_full_source_items_limit_is_independent_of_output_records() -> None:
    from structuraguard.contracts.structure import StructuralProfilingOptions

    request, batches = await prepared(
        DelimitedTextParser(), b"meta\nname,n\nAda,1\nBob,2\n"
    )
    options = ParsePlanOptions(
        source_limits=StructuralProfilingOptions(max_total_items=2)
    )
    result = ParsePlanValidator(options=options).validate(request, batches=batches)
    assert result.decision is ValidationDecision.REJECTED
    assert result.issues[0].message_key == "SOURCE_TOTAL_ITEMS"


@pytest.mark.anyio
async def test_output_byte_budget_fails_with_typed_issue() -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    options = ParsePlanOptions(max_output_batch_bytes=32)
    checked = (
        ParsePlanValidator(options=options)
        .validate(request, batches=batches)
        .validated_plan
    )
    assert checked is not None
    with pytest.raises(ParseExecutionError) as failure:
        await anext(
            ParsePlanExecutor(options=options).execute(
                stream(batches), checked, execution_context(request)
            )
        )
    assert failure.value.issue.code == "SECURITY_LIMIT_EXCEEDED"
    assert failure.value.issue.emitted_batches == 0
