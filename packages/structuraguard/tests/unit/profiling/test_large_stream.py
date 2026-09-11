"""Большой логический source потребляется лениво и останавливается у cap."""

from collections.abc import Iterator

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.profiling import NormalizedProfilingOptions
from structuraguard.exceptions import NormalizedProfilingError
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


async def test_ten_thousand_records_use_bounded_sketch_and_samples() -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            ({"id": IntegerScalar(value=i)} for i in range(10000)), batch_size=100
        )
    )
    assert result.record_count == 10000 and result.value_count == 10000
    assert result.fields[0].unique_mode == "estimated"
    assert len(result.fields[0].examples) == 8
    assert result.retained_state_bytes < 64 * 1024 * 1024
    assert result.sample_bytes <= 8 * 512


async def test_trillion_record_source_is_not_materialized_before_limit() -> None:
    generated = 0

    def rows() -> Iterator[dict[str, IntegerScalar]]:
        nonlocal generated
        for i in range(10**12):
            generated += 1
            yield {"id": IntegerScalar(value=i)}

    with pytest.raises(NormalizedProfilingError, match="LIMIT_EXCEEDED"):
        await NormalizedDataProfiler(NormalizedProfilingOptions(max_ids=100)).profile(
            normalized_stream(rows(), batch_size=10)
        )
    assert generated == 40
