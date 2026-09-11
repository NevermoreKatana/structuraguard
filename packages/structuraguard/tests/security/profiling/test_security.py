"""Trust boundary: corrupted streams, ограничение памяти, PII и lifecycle."""

import asyncio
from collections.abc import AsyncIterator

import pytest
from tests.fakes.profiling import make_batch, normalized_stream

from structuraguard.contracts.common import (
    DataClassification,
    IntegerScalar,
    StringScalar,
)
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.profiling import (
    ExamplePolicy,
    NormalizedProfileContext,
    NormalizedProfilingOptions,
    PIIClassificationRequest,
    PIIClassificationResult,
)
from structuraguard.exceptions import NormalizedProfilingError
from structuraguard.profiling import LocalPIIClassifier, NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "mode",
    [
        "no_terminal",
        "trailing",
        "reordered",
        "duplicate_ids",
        "forged_value",
        "legacy",
        "last_only",
    ],
)
async def test_corrupt_stream_never_completes_and_closes(mode: str) -> None:
    batches = [
        b
        async for b in normalized_stream(
            [{"x": IntegerScalar(value=1)}, {"x": IntegerScalar(value=2)}], batch_size=1
        )
    ]
    closed = False
    if mode == "no_terminal":
        batches = batches[:-1]
    elif mode == "trailing":
        batches.append(batches[0])
    elif mode == "reordered":
        batches[0], batches[1] = batches[1], batches[0]
    elif mode == "duplicate_ids":
        batches[1] = make_batch(batches[0].records, 1)
    elif mode == "legacy":
        batches[0] = batches[0].model_copy(update={"schema_version": "1.0.0"})
    elif mode == "last_only":
        batches = batches[-1:]
    else:
        record = batches[0].records[0]
        entity = record.entities[0]
        value = entity.values[0].model_copy(
            update={"normalized_value": StringScalar(value="secret.person@example.com")}
        )
        batches[0] = batches[0].model_copy(
            update={
                "records": (
                    record.model_copy(
                        update={
                            "entities": (
                                entity.model_copy(update={"values": (value,)}),
                            )
                        }
                    ),
                )
            }
        )

    async def stream() -> AsyncIterator[NormalizedBatch]:
        nonlocal closed
        try:
            for batch in batches:
                yield batch
        finally:
            closed = True

    with pytest.raises(NormalizedProfilingError) as captured:
        await NormalizedDataProfiler().profile(stream())
    assert closed
    assert "secret.person" not in str(captured.value)


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        (NormalizedProfilingOptions(max_ids=5), "global_ids"),
        (NormalizedProfilingOptions(max_batches=1), "batches"),
        (NormalizedProfilingOptions(max_fields=1), "fields"),
        (NormalizedProfilingOptions(max_state_bytes=100), "retained_state_bytes"),
        (NormalizedProfilingOptions(max_batch_bytes=100), "input_bytes"),
        (NormalizedProfilingOptions(max_batch_items=5), "batch_items"),
        (NormalizedProfilingOptions(max_manifest_bytes=100), "input_bytes"),
        (NormalizedProfilingOptions(max_profile_bytes=100), "profile_bytes"),
    ],
)
async def test_hard_limits_are_explicit(
    options: NormalizedProfilingOptions, reason: str
) -> None:
    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler(options).profile(
            normalized_stream(
                [{"a": IntegerScalar(value=1), "b": IntegerScalar(value=2)}] * 3,
                batch_size=1,
            )
        )
    assert caught.value.error_code == "NORMALIZED_PROFILE_LIMIT_EXCEEDED"
    assert caught.value.details["reason"] == reason


async def test_long_unicode_is_counted_but_not_retained_or_classified_public() -> None:
    text = "я🙂" * 3000
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(examples=ExamplePolicy.LOCAL_RAW)
    ).profile(normalized_stream([{"text": StringScalar(value=text)}]))
    field = result.fields[0]
    assert field.mean_length == 6000
    assert field.sample_skipped == 1 and not field.examples
    assert field.pattern_skipped == 1 and field.pii.state == "unknown"


async def test_late_pii_closes_previous_raw_samples_and_safe_summary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    values = [{"x": StringScalar(value=str(i))} for i in range(30)]
    values.append({"x": StringScalar(value="secret.person@example.org")})
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(examples=ExamplePolicy.LOCAL_RAW)
    ).profile(normalized_stream(values))
    assert all(e.masked and e.value is None for e in result.fields[0].examples)
    assert result.classification == DataClassification.CONFIDENTIAL
    assert (
        "secret.person"
        not in result.safe_summary().canonical_json() + repr(result) + caplog.text
    )


async def test_custom_classifier_cannot_downgrade_or_forge_binding() -> None:
    class Permissive:
        async def classify(
            self, request: PIIClassificationRequest
        ) -> PIIClassificationResult:
            baseline = await LocalPIIClassifier().classify(request)
            return baseline.model_copy(
                update={"classification": DataClassification.PUBLIC}
            )

    with pytest.raises(NormalizedProfilingError, match="CLASSIFICATION_FAILED"):
        await NormalizedDataProfiler(classifier=Permissive()).profile(
            normalized_stream([{"x": StringScalar(value="a@example.org")}])
        )


async def test_source_failure_message_is_not_reexposed() -> None:
    async def source() -> AsyncIterator[NormalizedBatch]:
        yield await anext(normalized_stream([]))
        raise RuntimeError("private.person@example.org")

    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler().profile(source())
    assert "private.person" not in str(caught.value)


async def test_cancellation_closes_owned_iterator() -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncIterator[NormalizedBatch]:
        try:
            entered.set()
            await asyncio.Event().wait()
            yield await anext(normalized_stream([]))
        finally:
            closed.set()

    task = asyncio.create_task(NormalizedDataProfiler().profile(source()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_cleanup_failure_prevents_completed_result() -> None:
    class BrokenClose:
        def __init__(self) -> None:
            self.stream = normalized_stream([])

        def __aiter__(self) -> "BrokenClose":
            return self

        async def __anext__(self) -> NormalizedBatch:
            return await anext(self.stream)

        async def aclose(self) -> None:
            await self.stream.aclose()
            raise RuntimeError("raw-secret")

    with pytest.raises(NormalizedProfilingError, match="CLEANUP_FAILED") as caught:
        await NormalizedDataProfiler().profile(BrokenClose())
    assert "raw-secret" not in str(caught.value)


async def test_controlled_deadline_stops_a_logically_huge_source() -> None:
    ticks = iter(range(100))
    with pytest.raises(NormalizedProfilingError, match="TIMEOUT"):
        await NormalizedDataProfiler(
            NormalizedProfilingOptions(max_processing_seconds=1),
            timer=lambda: float(next(ticks)),
        ).profile(
            normalized_stream({"x": IntegerScalar(value=i)} for i in range(10**12))
        )


async def test_source_classification_is_a_floor() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": IntegerScalar(value=1)}]),
        context=NormalizedProfileContext(
            data_classification=DataClassification.RESTRICTED
        ),
    )
    assert result.classification == DataClassification.RESTRICTED


async def test_duplicate_field_values_after_model_copy_are_rejected() -> None:
    batch = await anext(normalized_stream([{"x": IntegerScalar(value=1)}]))
    entity = batch.records[0].entities[0]
    changed = entity.model_copy(update={"values": entity.values * 2})
    record = batch.records[0].model_copy(update={"entities": (changed,)})
    forged = batch.model_copy(update={"records": (record,)})
    with pytest.raises(NormalizedProfilingError):
        await NormalizedDataProfiler().profile(forged)


async def test_forged_unicode_surrogate_is_a_safe_typed_error() -> None:
    batch = await anext(normalized_stream([{"x": IntegerScalar(value=1)}]))
    entity = batch.records[0].entities[0]
    value = entity.values[0].model_copy(update={"field_name": "\ud800private"})
    record = batch.records[0].model_copy(
        update={"entities": (entity.model_copy(update={"values": (value,)}),)}
    )
    forged = batch.model_copy(update={"records": (record,)})
    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler().profile(forged)
    assert "private" not in str(caught.value)


async def test_repeated_cancellation_waits_for_owned_cleanup() -> None:
    read_started = asyncio.Event()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    closed = asyncio.Event()

    class Source:
        def __aiter__(self) -> "Source":
            return self

        async def __anext__(self) -> NormalizedBatch:
            read_started.set()
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def aclose(self) -> None:
            close_started.set()
            await release_close.wait()
            closed.set()

    task = asyncio.create_task(NormalizedDataProfiler().profile(Source()))
    await read_started.wait()
    task.cancel()
    await close_started.wait()
    task.cancel()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_classifier_failure_is_redacted_and_source_closed() -> None:
    class Broken:
        async def classify(
            self, request: PIIClassificationRequest
        ) -> PIIClassificationResult:
            raise RuntimeError("name@example.org")

    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler(classifier=Broken()).profile(
            normalized_stream([{"x": IntegerScalar(value=1)}])
        )
    assert caught.value.error_code == "NORMALIZED_PROFILE_CLASSIFICATION_FAILED"
    assert "name@example.org" not in str(caught.value)


async def test_forged_manifest_preflight_error_remains_typed() -> None:
    terminal = await anext(normalized_stream([]))
    assert terminal.manifest is not None
    forged_source = terminal.source.model_copy(update={"artifact_id": "\ud800private"})
    forged = terminal.model_copy(
        update={
            "manifest": terminal.manifest.model_copy(update={"source": forged_source})
        }
    )
    with pytest.raises(NormalizedProfilingError):
        await NormalizedDataProfiler().profile(forged)


async def test_synchronous_close_failure_does_not_escape_boundary() -> None:
    class InvalidClose:
        def __init__(self) -> None:
            self.stream = normalized_stream([])

        def __aiter__(self) -> "InvalidClose":
            return self

        async def __anext__(self) -> NormalizedBatch:
            return await anext(self.stream)

        def aclose(self) -> None:
            raise RuntimeError("private-close@example.org")

    with pytest.raises(NormalizedProfilingError, match="CLEANUP_FAILED") as caught:
        await NormalizedDataProfiler().profile(InvalidClose())
    assert "private-close" not in str(caught.value)
