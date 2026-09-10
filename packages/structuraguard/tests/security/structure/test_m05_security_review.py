"""Безопасные reproductions findings текущего security review M5."""

import traceback
from collections.abc import AsyncIterator

import pytest
from tests.unit.structure.test_execution import (
    execute,
    execution_context,
    prepared,
    stream,
)

from structuraguard.contracts.execution import ParsePlanOptions, PhysicalValueOrigin
from structuraguard.contracts.parsing import ParseField, ParsePlanValidationRequest
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import (
    ParseExecutionError,
    ParserError,
    SecurityPolicyError,
    StructuraGuardError,
    StructuralProfilingError,
)
from structuraguard.parsers.builtin import DelimitedTextParser, JsonDocumentParser
from structuraguard.structure import (
    DeterministicStructureAnalyzer,
    ParsePlanExecutor,
    ParsePlanValidator,
    StructuralProfiler,
    _runtime,
)
from structuraguard.structure._plan_check import validation_fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize("component", ["profiler", "analyzer"])
@pytest.mark.parametrize("phase", ["open", "first_read", "late_read"])
async def test_source_exceptions_do_not_leak_into_structural_diagnostics(
    component: str, phase: str
) -> None:
    _, batches = await prepared(
        DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n", batch_size=1
    )

    class BrokenSource:
        closed = False
        reads = 0

        def __aiter__(self) -> AsyncIterator[ExtractedBatch]:
            if phase == "open":
                raise RuntimeError("secret-canary")
            return self

        async def __anext__(self) -> ExtractedBatch:
            self.reads += 1
            if phase == "late_read" and self.reads == 1:
                return batches[0]
            raise RuntimeError("secret-canary")

        async def aclose(self) -> None:
            self.closed = True

    source = BrokenSource()
    with pytest.raises(StructuralProfilingError) as failure:
        if component == "profiler":
            await StructuralProfiler().profile(source)
        else:
            await DeterministicStructureAnalyzer().analyze(source)
    assert failure.value.error_code == "STRUCTURE_INPUT_INVALID"
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert phase == "open" or source.closed


@pytest.mark.anyio
@pytest.mark.parametrize("budget", ["items", "bytes"])
@pytest.mark.parametrize("component", ["validator", "executor"])
async def test_record_budget_stops_fanout_before_materializing_all_field_values(
    budget: str,
    component: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, batches = await prepared(
        JsonDocumentParser(),
        b'[{"report":"daily","items":[{"value":"payload"},{"value":"payload"},{"value":"payload"},{"value":"payload"}]},'
        b'{"report":"night","items":[{"value":"payload"},{"value":"payload"},{"value":"payload"},{"value":"payload"}]}]',
    )
    payload = request.model_dump(mode="python")
    plan = payload["plan"]
    plan.update(analysis=None, fingerprint="sha256:" + "0" * 64)
    child = next(e for e in plan["entities"] if e["parent_entity_id"] is not None)
    child_field = next(f for f in plan["fields"] if f["field_id"] in child["field_ids"])
    repeated = tuple(
        {**child_field, "field_id": f"copy_{i}", "semantic_name": f"copy_{i}"}
        for i in range(24)
    )
    plan["fields"] = (
        tuple(f for f in plan["fields"] if f["field_id"] not in child["field_ids"])
        + repeated
    )
    child["field_ids"] = tuple(f["field_id"] for f in repeated)
    malicious = ParsePlanValidationRequest.model_validate(payload)
    baseline = ParsePlanValidator().validate(malicious, batches=batches).validated_plan
    assert baseline is not None
    copies = 0
    original = _runtime.copied

    def counted(
        field: ParseField, origin: PhysicalValueOrigin
    ) -> _runtime.SelectedValue:
        nonlocal copies
        copies += 1
        return original(field, origin)

    monkeypatch.setattr(_runtime, "copied", counted)
    options = (
        ParsePlanOptions(max_record_items=10)
        if budget == "items"
        else ParsePlanOptions(max_record_bytes=1200)
    )
    if component == "validator":
        result = await ParsePlanValidator(options=options).validate_source(
            malicious, stream(batches)
        )
        assert result.validated_plan is None
        assert result.issues[0].code == "SECURITY_LIMIT_EXCEEDED"
    else:
        forged = baseline.model_copy(
            update={
                "validation_fingerprint": validation_fingerprint(malicious, options)
            }
        )
        with pytest.raises(ParseExecutionError) as failure:
            await anext(
                ParsePlanExecutor(options=options).execute(
                    stream(batches), forged, execution_context(malicious)
                )
            )
        assert failure.value.issue.code == "SECURITY_LIMIT_EXCEEDED"
        assert failure.value.issue.emitted_batches == 0
    # Тест ограничен 97 allocations даже до исправления; production fan-out
    # существенно больше. Отказ должен наступить до накопления всех values.
    assert copies <= 10


@pytest.mark.anyio
@pytest.mark.parametrize("entrypoint", ["one_batch", "stream"])
async def test_normalized_verification_rejects_a_forged_manifest_fingerprint(
    entrypoint: str,
) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    output = await execute(request, batches)
    original = output[-1].manifest
    assert original is not None
    forged = original.model_copy(
        update={"normalized_fingerprint": "sha256:" + "f" * 64}
    )
    terminal = output[-1].model_copy(update={"manifest": forged})
    with pytest.raises(ValueError, match="fingerprint"):
        if entrypoint == "one_batch":
            forged.validate_batch(terminal)
        else:
            forged.validate_batches((*output[:-1], terminal))


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code", ["SECURITY_LIMIT_EXCEEDED", "PARSER_MALFORMED_INPUT", "SECRET_CANARY"]
)
async def test_source_sdk_error_preserves_only_allowlisted_code(code: str) -> None:
    class BrokenSource:
        def __aiter__(self) -> AsyncIterator[ExtractedBatch]:
            return self

        async def __anext__(self) -> ExtractedBatch:
            raise StructuraGuardError(
                error_code=code,
                message="secret-canary",
                details={"sample": "secret-canary"},
            )

    with pytest.raises(StructuralProfilingError) as failure:
        await StructuralProfiler().profile(BrokenSource())
    assert failure.value.error_code == (
        "STRUCTURE_INPUT_INVALID" if code == "SECRET_CANARY" else code
    )
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert failure.value.details == {"reason": "source_read_failed"}


@pytest.mark.anyio
@pytest.mark.parametrize("error_type", [ParserError, SecurityPolicyError])
async def test_source_error_sanitization_preserves_parser_and_security_categories(
    error_type: type[StructuraGuardError],
) -> None:
    class BrokenSource:
        def __aiter__(self) -> AsyncIterator[ExtractedBatch]:
            return self

        async def __anext__(self) -> ExtractedBatch:
            raise error_type(
                error_code="SECURITY_INPUT_REJECTED", message="secret-canary"
            )

    with pytest.raises(error_type) as failure:
        await StructuralProfiler().profile(BrokenSource())
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
