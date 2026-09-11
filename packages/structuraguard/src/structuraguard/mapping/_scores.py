"""Чистая арифметика score, независимая от Decimal context вызывающего кода."""

from collections.abc import Iterable
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal

from structuraguard.contracts.deterministic_mapping import (
    CompatibilityStatus,
    DeterministicMappingOptions,
)


def quantize(value: Decimal) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return value.quantize(Decimal("0.000001"))


def ratio(numerator: int, denominator: int) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return quantize(Decimal(numerator) / denominator) if denominator else Decimal(0)


def product(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return (left * right).quantize(Decimal("0.000000000001"))


def total(values: Iterable[Decimal]) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return quantize(sum(values, Decimal(0)))


def difference(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return quantize(left - right)


def final_score(base: Decimal, ambiguity: Decimal, validation: Decimal) -> Decimal:
    return max(
        Decimal(0), min(Decimal(1), difference(difference(base, ambiguity), validation))
    )


def average(values: tuple[Decimal, ...]) -> Decimal:
    with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
        return quantize(sum(values, Decimal(0)) / len(values)) if values else Decimal(0)


def candidate_status(
    score: Decimal,
    compatibility: CompatibilityStatus,
    blockers: tuple[str, ...],
    options: DeterministicMappingOptions,
) -> Literal["auto_candidate", "review", "rejected"]:
    """Чистое решение по опубликованному score; blockers сильнее thresholds."""
    if (
        score >= options.auto_threshold
        and compatibility == "compatible"
        and not blockers
    ):
        return "auto_candidate"
    return "review" if score >= options.review_threshold else "rejected"
