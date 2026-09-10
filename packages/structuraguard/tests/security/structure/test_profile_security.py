"""Trust boundary profiler: безопасные failures и контроль до allocation."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import pytest
from pydantic import ValidationError
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.contracts import ExtractedBatch, StructureProfile
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.exceptions import SecurityPolicyError, StructuralProfilingError
from structuraguard.parsers.builtin import PlainTextParser
from structuraguard.structure import StructuralProfiler


async def batches_for(
    content: bytes, *, batch_size: int = 1
) -> tuple[ExtractedBatch, ...]:
    source = source_for(content, display_name="text")
    return await collect(
        PlainTextParser(),
        source,
        contexts_for(source, content, batch_size=batch_size)[1],
    )


async def stream(batches: tuple[ExtractedBatch, ...]) -> AsyncIterator[ExtractedBatch]:
    for batch in batches:
        yield batch


@pytest.mark.anyio
async def test_forged_nested_dto_and_stale_hash_are_rejected_without_raw_error() -> (
    None
):
    batches = await batches_for(b"original\n")
    line = batches[0].lines[0].model_copy(update={"text": "secret-canary"})
    forged = batches[0].model_copy(update={"lines": (line,)})
    with pytest.raises(StructuralProfilingError) as failure:
        await StructuralProfiler().profile(forged)
    assert failure.value.error_code == "STRUCTURE_INPUT_INVALID"
    assert "secret-canary" not in str(failure.value) + repr(failure.value.details)


@pytest.mark.anyio
async def test_incomplete_model_construct_has_typed_error() -> None:
    with pytest.raises(StructuralProfilingError, match="STRUCTURE_INPUT_INVALID"):
        await StructuralProfiler().profile(
            ExtractedBatch.model_construct(batch_index=0)
        )


@pytest.mark.anyio
async def test_preflight_rejects_giant_forged_value_before_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batches = await batches_for(b"original\n")
    line = batches[0].lines[0].model_copy(update={"text": "x" * 100_000})
    forged = batches[0].model_copy(update={"lines": (line,)})

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("serialization before preflight")

    monkeypatch.setattr(ExtractedBatch, "model_dump", forbidden)
    with pytest.raises(SecurityPolicyError) as failure:
        await StructuralProfiler(
            options=StructuralProfilingOptions(max_batch_bytes=4096)
        ).profile(forged)
    assert failure.value.details["resource"] == "profile_batch_bytes"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation", ["missing", "reordered", "duplicate", "after_terminal"]
)
async def test_incomplete_and_reordered_streams_fail_closed(mutation: str) -> None:
    batches = await batches_for(b"one\ntwo\nthree\n")
    changed = {
        "missing": batches[:-1],
        "reordered": (batches[1], batches[0], batches[2]),
        "duplicate": (batches[0], batches[0], *batches[1:]),
        "after_terminal": (*batches, batches[0]),
    }[mutation]
    with pytest.raises(StructuralProfilingError):
        await StructuralProfiler().profile(stream(changed))


@pytest.mark.anyio
async def test_total_physical_budget_is_independent_of_sampling() -> None:
    batches = await batches_for(b"one\ntwo\nthree\n")
    with pytest.raises(SecurityPolicyError) as failure:
        await StructuralProfiler(
            options=StructuralProfilingOptions(
                max_sample_items=1, max_physical_objects=2
            )
        ).profile(stream(batches))
    assert failure.value.details["resource"] == "profile_physical_objects"


@pytest.mark.anyio
async def test_cancellation_closes_owned_iterator() -> None:
    batch = (await batches_for(b"one\ntwo\n"))[0]
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batch
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(StructuralProfiler().profile(source()))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.anyio
async def test_timeout_has_typed_outcome_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = (await batches_for(b"one\ntwo\n"))[0]
    waiting = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batch
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(
        StructuralProfiler(
            options=StructuralProfilingOptions(max_processing_seconds=1)
        ).profile(source())
    )
    await waiting.wait()
    loop = asyncio.get_running_loop()
    expired = loop.time() + 2
    with monkeypatch.context() as clock:
        clock.setattr(loop, "time", lambda: expired)
        with pytest.raises(StructuralProfilingError, match="PROCESSING_TIMEOUT"):
            await task
    assert closed.is_set()


@pytest.mark.parametrize(
    "field",
    [
        "max_sample_items",
        "max_sample_bytes",
        "max_batches",
        "max_depth",
        "max_processing_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1, True, 2**63])
def test_options_reject_unbounded_and_non_strict_values(
    field: str, value: int | bool
) -> None:
    with pytest.raises(ValidationError):
        StructuralProfilingOptions.model_validate({field: value})


@pytest.mark.anyio
async def test_profile_fingerprint_rejects_forged_coverage() -> None:
    profile = await StructuralProfiler().profile(
        stream(await batches_for(b"one\ntwo\n"))
    )
    payload = profile.model_dump(mode="python")
    payload["candidates"] = ()
    with pytest.raises(ValidationError):
        StructureProfile.model_validate(payload)


@pytest.mark.anyio
async def test_tree_depth_limit_applies_outside_retained_sample() -> None:
    from tests.unit.structure.test_profiling import profile_content

    from structuraguard.parsers.builtin import JsonDocumentParser

    with pytest.raises(SecurityPolicyError) as failure:
        await profile_content(
            JsonDocumentParser(),
            b'[{"a":{"b":{"c":1}}}]',
            options=StructuralProfilingOptions(max_sample_items=1, max_depth=2),
        )
    assert failure.value.details["resource"] == "profile_tree_depth"


@pytest.mark.anyio
async def test_network_is_not_used_and_regex_text_stays_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Profiler attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    batches = await batches_for(b"INFO (a+)+$ x=12\nINFO (a+)+$ x=13\n")
    profile = await StructuralProfiler().profile(stream(batches))
    assert "(a+)+$" in profile.canonical_json()
    assert all(
        "regex" not in type(item.observation).model_fields
        for item in profile.observations
    )


@pytest.mark.anyio
async def test_candidate_links_are_exact_and_not_only_shared_table_refs() -> None:
    from tests.unit.structure.test_profiling import profile_content

    from structuraguard.parsers.builtin import DelimitedTextParser

    profile = await profile_content(DelimitedTextParser(), b"red,green\nblue,white\n")
    by_id = {item.evidence_id: item for item in profile.observations}
    assert len(profile.candidates) >= 2
    for candidate in profile.candidates:
        assert candidate.observation_ids
        assert all(identifier in by_id for identifier in candidate.observation_ids)
    assert len({candidate.observation_ids for candidate in profile.candidates}) > 1


@pytest.mark.anyio
async def test_cleanup_failure_is_reported_without_replacing_primary_or_leaking_values() -> (
    None
):
    import traceback

    batch = (await batches_for(b"one\ntwo\n"))[0].model_copy(update={"batch_index": 3})

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batch
        finally:
            raise RuntimeError("secret-canary")

    with pytest.raises(StructuralProfilingError) as failure:
        await StructuralProfiler().profile(source())
    assert failure.value.error_code == "STRUCTURE_INPUT_INVALID"
    diagnostic = "".join(traceback.format_exception(failure.value))
    assert "iterator_cleanup" in diagnostic
    assert "secret-canary" not in diagnostic
