"""KMV и occurrence sampling: bounded heaps без ссылок на весь batch."""

import hashlib
import heapq
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from structuraguard.contracts._base import canonical_json_value
from structuraguard.contracts.common import NormalizedScalar
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import ExamplePolicy, NormalizedProfilingOptions
from structuraguard.profiling._stream import Ledger


def ratio(numerator: int | Decimal, denominator: int) -> Decimal | None:
    if denominator == 0:
        return None
    with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
        return (Decimal(numerator) / denominator).quantize(Decimal("0.000001"))


class Distinct:
    def __init__(self, k: int, ledger: Ledger) -> None:
        self.k = k
        self.ledger = ledger
        self.values: set[int] = set()
        self.heap: list[int] = []
        self.overflow = False

    def add(self, encoded: bytes) -> None:
        value = int.from_bytes(hashlib.sha256(encoded).digest()[:16], "big")
        if value in self.values:
            return
        if len(self.values) < self.k:
            self.ledger.add(192)
            self.values.add(value)
            heapq.heappush(self.heap, -value)
            return
        self.overflow = True
        if value < -self.heap[0]:
            previous = -heapq.heapreplace(self.heap, -value)
            self.values.remove(previous)
            self.values.add(value)

    def count(self, non_null: int) -> int | None:
        if not non_null:
            return None
        if not self.overflow:
            return len(self.values)
        numerator = (self.k - 1) * (2**128 + 1)
        denominator = -self.heap[0] + 1
        return min(
            non_null, max(self.k + 1, (numerator + denominator // 2) // denominator)
        )


class Samples:
    def __init__(
        self,
        options: NormalizedProfilingOptions,
        field: SemanticFieldRef,
        ledger: Ledger,
    ) -> None:
        self.options = options
        self.ledger = ledger
        self.prefix = canonical_json_value(
            ("normalized_samples_v1", options.seed, field.entity_type, field.field_name)
        ).encode()
        self.heap: list[tuple[int, int, NormalizedScalar, int]] = []
        self.eligible = self.skipped = self.bytes = 0

    def add(self, value: NormalizedScalar, ordinal: int, encoded: bytes) -> None:
        if len(encoded) > self.options.max_example_bytes:
            self.skipped += 1
            return
        self.eligible += 1
        k = self.options.examples_per_field
        if not k or self.options.examples == ExamplePolicy.OMIT:
            return
        priority = int.from_bytes(
            hashlib.sha256(self.prefix + b"\x00" + str(ordinal).encode()).digest(),
            "big",
        )
        if len(self.heap) == k and (priority, ordinal) >= (
            -self.heap[0][0],
            -self.heap[0][1],
        ):
            return
        size = len(encoded)
        previous_size = self.heap[0][3] if len(self.heap) == k else 0
        self.ledger.add((size - previous_size) * 4 + (512 if len(self.heap) < k else 0))
        entry = (-priority, -ordinal, value, size)
        if len(self.heap) == k:
            heapq.heapreplace(self.heap, entry)
        else:
            heapq.heappush(self.heap, entry)
        self.bytes += size - previous_size
