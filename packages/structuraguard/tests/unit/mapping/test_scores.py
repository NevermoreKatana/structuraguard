"""Точные пороги и SDK contributions без provider confidence."""

from decimal import Decimal, localcontext

import pytest

from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.mapping._scores import (
    average,
    candidate_status,
    difference,
    final_score,
)


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.699999", "rejected"),
        ("0.700000", "review"),
        ("0.899999", "review"),
        ("0.900000", "auto_candidate"),
    ],
)
def test_threshold_boundaries_are_inclusive(score: str, expected: str) -> None:
    assert (
        candidate_status(
            Decimal(score), "compatible", (), DeterministicMappingOptions()
        )
        == expected
    )


def test_blockers_override_even_perfect_scores_and_average_does_not_overflow() -> None:
    options = DeterministicMappingOptions()
    assert (
        candidate_status(
            Decimal(1), "conditional", ("TRANSFORMATION_REQUIRED",), options
        )
        == "review"
    )
    assert (
        candidate_status(Decimal(1), "compatible", ("FK_UNRESOLVED",), options)
        == "review"
    )
    with localcontext() as ctx:
        ctx.prec = 2
        assert average((Decimal(1),) * 6) == 1
        assert difference(Decimal("0.9"), Decimal("0.800001")) == Decimal("0.099999")
        assert final_score(Decimal("0.05"), Decimal("0.1"), Decimal("0.1")) == 0
