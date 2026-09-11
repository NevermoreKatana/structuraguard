"""Регрессии финального review: manifest boundary и полный размер результата."""

from collections.abc import AsyncIterator

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar, StringScalar
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.profiling import NormalizedProfilingOptions
from structuraguard.exceptions import NormalizedProfilingError
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("variant", ["scalar", "null_batches"])
async def test_forged_manifest_returns_safe_typed_error_and_closes(
    variant: str,
) -> None:
    terminal = await anext(normalized_stream([]))
    assert terminal.manifest is not None
    manifest = (
        StringScalar(value="private-review@example.org")
        if variant == "scalar"
        else terminal.manifest.model_copy(update={"batches": None})
    )
    forged = terminal.model_copy(update={"manifest": manifest})
    closed = False

    async def source() -> AsyncIterator[NormalizedBatch]:
        nonlocal closed
        try:
            yield forged
        finally:
            closed = True

    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler().profile(source())
    assert caught.value.error_code == "NORMALIZED_PROFILE_INVALID_STREAM"
    assert caught.value.details == {"reason": "batch_contract"}
    assert "private-review" not in str(caught.value)
    assert closed


@pytest.mark.parametrize("name_prefix", ["f", "поле🙂"])
async def test_full_serialized_profile_is_rejected_above_output_cap(
    name_prefix: str,
) -> None:
    rows = [{f"{name_prefix}{i}": IntegerScalar(value=i) for i in range(100)}]
    reference = await NormalizedDataProfiler().profile(normalized_stream(rows))
    assert len(reference.relationships) == 4096
    required = len(reference.canonical_json().encode("utf-8"))
    closed = False

    async def source() -> AsyncIterator[NormalizedBatch]:
        nonlocal closed
        try:
            async for batch in normalized_stream(rows):
                yield batch
        finally:
            closed = True

    # Hash options меняется, но имеет прежнюю фиксированную длину.
    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler(
            NormalizedProfilingOptions(max_profile_bytes=required - 1)
        ).profile(source())
    assert caught.value.error_code == "NORMALIZED_PROFILE_LIMIT_EXCEEDED"
    assert caught.value.details == {"reason": "profile_bytes"}
    assert closed

    options = NormalizedProfilingOptions(max_profile_bytes=required + 4096)
    accepted = await NormalizedDataProfiler(options).profile(normalized_stream(rows))
    assert len(accepted.canonical_json().encode("utf-8")) <= options.max_profile_bytes
    assert accepted.fields == reference.fields
    assert accepted.relationships == reference.relationships
    assert accepted.normalized_data_fingerprint == reference.normalized_data_fingerprint
