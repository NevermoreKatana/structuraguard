"""Независимый полный sort служит oracle для bounded heap с неравными scores."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.mapping import column, table

from structuraguard.contracts.database import CatalogColumnRef
from structuraguard.contracts.deterministic_mapping import CandidateSignal
from structuraguard.mapping._compatibility import Compatibility, PatternMatch
from structuraguard.mapping._names import NameEvidence, normalize_name
from structuraguard.mapping._ranking import BaseEvidence, Ranked, Target, TopCandidates


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(0, 1_000_000), min_size=2, max_size=40),
    st.sampled_from((1, 5, 10)),
)
def test_heap_matches_independent_exhaustive_sort(scores: list[int], k: int) -> None:
    heap = TopCandidates(max(k, 2))
    evidence = BaseEvidence(
        NameEvidence(),
        Decimal(0),
        Compatibility("compatible", Decimal(1)),
        PatternMatch(),
    )
    for i, raw in enumerate(scores):
        c = column("email")
        t = table(f"t{i:03}", c)
        target = Target(
            t,
            c,
            CatalogColumnRef(table_id=t.table_id, column_id=c.column_id),
            normalize_name(c.name),
            (),
            (),
            (),
        )
        value = Decimal(raw) / Decimal(1_000_000)
        heap.add(
            Ranked(
                target,
                evidence,
                (
                    CandidateSignal(
                        code="name_similarity",
                        value=value,
                        weight=Decimal(1),
                        contribution=value,
                    ),
                ),
                (),
            )
        )
        assert len(heap.heap) <= max(k, 2)
    expected = sorted(enumerate(scores), key=lambda pair: (-pair[1], pair[0]))
    actual = heap.ordered()
    assert [item.target.table.name for item in actual] == [
        f"t{i:03}" for i, _ in expected[: max(k, 2)]
    ]
    assert heap.count == len(scores)
    assert heap.tie_count == scores.count(max(scores))
    assert actual[0].score - actual[1].score == Decimal(
        expected[0][1] - expected[1][1]
    ) / Decimal(1_000_000)
