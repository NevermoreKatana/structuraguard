"""Инкрементальные field accumulators без raw dataset и повторного прохода."""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from structuraguard.contracts._base import canonical_json_value
from structuraguard.contracts.common import NormalizedScalar
from structuraguard.contracts.normalized import NormalizedValue, SemanticFieldRef
from structuraguard.contracts.profiling import (
    IdentityHint,
    NormalizedProfilingOptions,
    PatternCode,
    PatternEvidence,
    PIICategory,
    ProfileCount,
    ProfileReason,
    ScalarExtrema,
    TypeCandidate,
    TypeInference,
    TypeKind,
)
from structuraguard.profiling._bounded import Distinct, Samples, ratio
from structuraguard.profiling._patterns import scan_string
from structuraguard.profiling._stream import Ledger


def _less(left: NormalizedScalar, right: NormalizedScalar) -> bool:
    a, b = left.value, right.value
    if isinstance(a, str) and isinstance(b, str):
        return a < b
    if isinstance(a, int | Decimal | float) and isinstance(b, int | Decimal | float):
        return a < b
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a < b
    if isinstance(a, date) and isinstance(b, date):
        return a < b
    return False


class Extrema:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self.metrics: dict[str, ScalarExtrema] = {}
        self.sizes: dict[str, int] = {}
        self.counts: Counter[str] = Counter()

    def add(self, value: NormalizedScalar, *, combine_numeric: bool = True) -> None:
        if value.kind == "null":
            return
        kind = (
            "numeric"
            if combine_numeric and value.kind in ("integer", "decimal")
            else value.kind
        )
        self.counts[kind] += 1
        previous = self.metrics.get(kind)
        low = (
            value
            if previous is None or _less(value, previous.minimum)
            else previous.minimum
        )
        high = (
            value
            if previous is None or _less(previous.maximum, value)
            else previous.maximum
        )
        if (
            previous is not None
            and low is previous.minimum
            and high is previous.maximum
        ):
            return
        item = ScalarExtrema(kind=kind, minimum=low, maximum=high)
        size = 1024 + 4 * len(item.canonical_json().encode())
        self.ledger.add(size - self.sizes.get(kind, 0))
        self.sizes[kind] = size
        self.metrics[kind] = item

    def output(self) -> tuple[ScalarExtrema, ...]:
        return tuple(
            self.metrics[k].model_copy(update={"count": self.counts[k]})
            for k in sorted(self.metrics)
        )


class FieldStats:
    def __init__(
        self,
        ref: SemanticFieldRef,
        declared: str,
        options: NormalizedProfilingOptions,
        ledger: Ledger,
    ) -> None:
        self.ref, self.declared, self.options = ref, declared, options
        ledger.add(8192)
        self.kinds: Counter[str] = Counter()
        self.present = self.nulls = self.non_null = self.strings = self.total_length = 0
        self.min_length: int | None = None
        self.max_length: int | None = None
        self.checked = self.skipped = 0
        self.extrema = Extrema(ledger)
        self.parsed = Extrema(ledger)
        self.distinct = Distinct(options.distinct_k, ledger)
        self.samples = Samples(options, ref, ledger)
        self.patterns: Counter[PatternCode] = Counter()
        self.candidates: Counter[TypeKind] = Counter()
        self.reasons: set[ProfileReason] = set()
        self.categories: set[PIICategory] = set()
        self.currencies: set[str] = set()

    def add(self, item: NormalizedValue) -> None:
        value = item.normalized_value
        self.present += 1
        self.kinds[value.kind] += 1
        if item.issue_codes:
            self.reasons.add("parse_issues")
        if value.kind == "null":
            self.nulls += 1
            return
        self.non_null += 1
        encoded = canonical_json_value(value).encode("utf-8")
        self.distinct.add(encoded)
        self.samples.add(value, self.non_null, encoded)
        self.extrema.add(value)
        if value.kind != "string":
            self.checked += 1
            self.candidates[value.kind] += 1
            if self.declared == "money":
                self.candidates[value.kind] -= 1
                self.candidates["money"] += 1
                self.patterns["money"] += 1
                self.categories.add("financial")
            return
        self.strings += 1
        length = len(value.value)
        self.total_length += length
        self.min_length = (
            length if self.min_length is None else min(length, self.min_length)
        )
        self.max_length = (
            length if self.max_length is None else max(length, self.max_length)
        )
        if (
            len(value.value.encode("utf-8")) > self.options.max_pattern_bytes
            or sum(c.isdigit() for c in value.value) > self.options.max_numeric_digits
        ):
            self.skipped += 1
            self.reasons.add("pattern_scan_skipped")
            return
        self.checked += 1
        name = self.ref.field_name.casefold()
        money_hint = self.declared == "money" or any(
            word in name for word in ("amount", "price", "cost", "сумм", "цен", "стоим")
        )
        scan = scan_string(value.value, self.options.locale, money_hint=money_hint)
        self.patterns.update(scan.patterns)
        self.candidates.update(scan.candidates)
        self.reasons.update(scan.reasons)
        self.categories.update(scan.categories)
        self.currencies.update(scan.currencies)
        if len(self.currencies) > 1:
            self.reasons.add("currency_ambiguity")
        for parsed in scan.parsed:
            self.parsed.add(parsed, combine_numeric=False)
        if not scan.candidates:
            self.candidates["string"] += 1

    def evidence(self) -> tuple[PatternEvidence, ...]:
        return tuple(
            PatternEvidence(code=k, count=v)
            for k, v in sorted(self.patterns.items())
            if v
        )

    def inference(self) -> TypeInference:
        reasons = set(self.reasons)
        counts = self.candidates.copy()
        observed = set(self.kinds) - {"null"}
        if len(observed) > 1 and not observed <= {"integer", "decimal"}:
            reasons.add("mixed_kinds")
        if observed <= {"integer", "decimal"} and "decimal" in observed:
            counts["decimal"] += counts.pop("integer", 0)
        candidates = tuple(
            TypeCandidate(
                kind=k, count=v, support=ratio(v, self.non_null) or Decimal(0)
            )
            for k, v in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
            if v > 0
        )
        if (
            self.declared
            in {"integer", "number", "decimal", "boolean", "date", "datetime", "money"}
            and candidates
            and candidates[0].kind != self.declared
        ):
            reasons.add("declared_type_conflict")
        declared_patterns: dict[str, tuple[PatternCode, ...]] = {
            "email": ("email",),
            "phone": ("phone",),
            "uuid": ("uuid",),
            "url": ("url",),
            "inn": ("russian_inn_10", "russian_inn_12"),
            "russian_inn_10": ("russian_inn_10",),
            "russian_inn_12": ("russian_inn_12",),
        }
        expected_patterns = declared_patterns.get(self.declared)
        if expected_patterns is not None and self.non_null:
            support_num, support_den = self.options.type_support.as_integer_ratio()
            if (
                sum(self.patterns[p] for p in expected_patterns) * support_den
                < self.non_null * support_num
            ):
                reasons.add("declared_type_conflict")
        blockers = reasons & {
            "mixed_kinds",
            "locale_ambiguity",
            "declared_type_conflict",
            "currency_ambiguity",
            "naive_datetime",
            "parse_issues",
        }
        if blockers:
            return TypeInference(
                status="ambiguous",
                candidates=candidates,
                reasons=tuple(sorted(reasons)),
            )
        if not self.non_null:
            return TypeInference(
                status="insufficient_evidence", reasons=("empty_field",)
            )
        if self.skipped or not candidates:
            reasons.add("insufficient_evidence")
            return TypeInference(
                status="insufficient_evidence",
                candidates=candidates,
                reasons=tuple(sorted(reasons)),
            )
        first = candidates[0]
        second = candidates[1].count if len(candidates) > 1 else 0
        # Decision использует counts и точные Decimal thresholds, не округлённый support.
        support_num, support_den = self.options.type_support.as_integer_ratio()
        margin_num, margin_den = self.options.type_margin.as_integer_ratio()
        dominant = first.count * support_den >= support_num * self.non_null
        margin = (first.count - second) * margin_den >= margin_num * self.non_null
        if not dominant or not margin:
            reasons.add("competing_types")
            return TypeInference(
                status="ambiguous",
                candidates=candidates,
                reasons=tuple(sorted(reasons)),
            )
        if (
            observed == {"string"}
            and first.kind != "string"
            and self.non_null < self.options.min_type_observations
        ):
            reasons.add("insufficient_evidence")
            return TypeInference(
                status="insufficient_evidence",
                candidates=candidates,
                reasons=tuple(sorted(reasons)),
            )
        return TypeInference(
            status="resolved",
            inferred_type=first.kind,
            candidates=candidates,
            reasons=tuple(sorted(reasons)),
        )

    def identity(self, entity_count: int, inference: TypeInference) -> IdentityHint:
        name = self.ref.field_name.casefold()
        name_hint = name in {
            "id",
            "uuid",
            "guid",
            "inn",
            "инн",
            "email",
            "code",
            "код",
        } or name.endswith(("_id", "_code"))
        pattern_hint = any(
            self.patterns[p]
            for p in ("email", "uuid", "russian_inn_10", "russian_inn_12")
        )
        integral = set(self.kinds) - {"null"} == {"integer"}
        if not (name_hint or pattern_hint or integral):
            return IdentityHint()
        signals: list[LiteralIdentitySignal] = []
        if name_hint:
            signals.append("name")
        if pattern_hint:
            signals.append("pattern")
        if integral:
            signals.append("integral")
        no_nulls = self.non_null == entity_count
        distinct = self.distinct.count(self.non_null)
        if no_nulls:
            signals.append("no_nulls")
        exact_unique = not self.distinct.overflow and distinct == self.non_null
        if exact_unique:
            signals.append("unique")
        kind: Literal["natural_key", "code", "identifier"] = (
            "natural_key"
            if pattern_hint and not self.patterns["uuid"]
            else "code"
            if "code" in name or name == "код"
            else "identifier"
        )
        if self.non_null < 20:
            return IdentityHint(
                kind=kind, strength="insufficient_evidence", signals=tuple(signals)
            )
        if (
            no_nulls
            and exact_unique
            and (name_hint or pattern_hint)
            and inference.status == "resolved"
            and not self.reasons
        ):
            return IdentityHint(kind=kind, strength="candidate", signals=tuple(signals))
        if no_nulls and distinct is not None and distinct * 100 >= self.non_null * 95:
            return IdentityHint(
                kind=kind, strength="possible_identifier", signals=tuple(signals)
            )
        return IdentityHint(kind=kind, signals=tuple(signals))

    def kind_counts(self) -> tuple[ProfileCount, ...]:
        return tuple(
            ProfileCount(name=k, count=v) for k, v in sorted(self.kinds.items())
        )


type LiteralIdentitySignal = Literal[
    "name", "pattern", "integral", "unique", "no_nulls"
]
