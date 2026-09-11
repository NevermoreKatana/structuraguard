"""Ленивый benchmark M8; profiling allocations измеряются только по запросу."""

import argparse
import asyncio
import json
import platform
import sys
import tracemalloc
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter

# Fixture находится в repository tests; в установленном SDK этот script не нужен.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/structuraguard"))

from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import (
    IntegerScalar,
    NormalizedScalar,
    StringScalar,
)
from structuraguard.contracts.profiling import NormalizedProfilingOptions
from structuraguard.profiling import NormalizedDataProfiler


async def benchmark(
    records: int, batch_size: int, *, allocations: bool, scenario: str
) -> None:
    """Напечатать только безопасные counters, timings и allocation envelope."""

    def rows() -> Iterator[dict[str, NormalizedScalar]]:
        for i in range(records):
            yield {
                "id": IntegerScalar(value=i),
                "category": StringScalar(
                    value="🙂" * 1024
                    if scenario == "unicode"
                    else str(i)
                    if scenario == "distinct"
                    else str(i % 10)
                ),
            }

    options = NormalizedProfilingOptions(
        max_processing_seconds=120 if allocations else 30
    )
    if allocations:
        tracemalloc.start()
    start = perf_counter()
    result = await NormalizedDataProfiler(options).profile(
        normalized_stream(rows(), batch_size=batch_size)
    )
    elapsed = perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1] if allocations else None
    if allocations:
        tracemalloc.stop()
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "records": records,
                "batch_size": batch_size,
                "scenario": scenario,
                "elapsed_seconds": round(elapsed, 3),
                "values_per_second": round(result.value_count / elapsed),
                "retained_ledger_bytes": result.retained_state_bytes,
                "sample_bytes": result.sample_bytes,
                "peak_tracemalloc_bytes": peak,
            },
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, default=10000)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--allocations", action="store_true")
    parser.add_argument(
        "--scenario", choices=("small", "distinct", "unicode"), default="small"
    )
    args = parser.parse_args()
    if not 1 <= args.records <= 50000 or not 1 <= args.batch_size <= 1000:
        parser.error("records: 1..50000; batch-size: 1..1000")
    asyncio.run(
        benchmark(
            args.records,
            args.batch_size,
            allocations=args.allocations,
            scenario=args.scenario,
        )
    )


if __name__ == "__main__":
    main()
