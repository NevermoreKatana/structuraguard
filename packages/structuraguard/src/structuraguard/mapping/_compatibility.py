"""Чистая консервативная совместимость по агрегатам M8 и metadata M7."""

from dataclasses import dataclass
from decimal import Decimal

from structuraguard.contracts.common import DecimalScalar, IntegerScalar, NumberScalar
from structuraguard.contracts.database import ColumnCatalog, DatabaseType
from structuraguard.contracts.deterministic_mapping import CompatibilityStatus
from structuraguard.contracts.profiling import NormalizedFieldProfile

from ._aliases import concepts
from ._names import normalize_name
from ._scores import ratio


@dataclass(frozen=True)
class Compatibility:
    status: CompatibilityStatus
    score: Decimal
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PatternMatch:
    score: Decimal = Decimal(0)
    available: bool = False
    coverage: Decimal = Decimal(0)
    blockers: tuple[str, ...] = ()


def expected_patterns(
    column: ColumnCatalog, semantic_type: str | None
) -> tuple[str, ...]:
    name = concepts(normalize_name(semantic_type or column.name).tokens)
    semantic = "_".join(name)
    if semantic in {
        "email",
        "phone",
        "url",
        "uuid",
        "date",
        "datetime",
        "money",
        "currency",
        "boolean",
        "integer",
        "decimal",
        "free_text",
    }:
        return (semantic,)
    if semantic in {"inn", "tax_id", "russian_inn"}:
        return ("russian_inn_10", "russian_inn_12")
    if semantic in {"identifier", "integer_id", "id"}:
        return ("identity",)
    if semantic in {"categorical", "category"}:
        return ("categorical",)
    if column.type_name in {
        "date",
        "datetime",
        "uuid",
        "boolean",
        "integer",
        "decimal",
    }:
        return (column.type_name,)
    return ()


def pattern_match(
    field: NormalizedFieldProfile, expected: tuple[str, ...]
) -> PatternMatch:
    if not expected or not field.non_null_count:
        return PatternMatch()
    counts: dict[str, int] = {p.code: p.count for p in field.patterns}
    native_counts = {kind.name: kind.count for kind in field.observed_kinds}
    for code in ("integer", "decimal", "boolean", "date", "datetime"):
        counts[code] = max(counts.get(code, 0), native_counts.get(code, 0))
    counts["decimal"] = max(
        counts.get("decimal", 0),
        native_counts.get("integer", 0) + native_counts.get("decimal", 0),
    )
    if field.declared_semantic_type == "money":
        counts["money"] = max(counts.get("money", 0), native_counts.get("decimal", 0))
    count = max((counts.get(code, 0) for code in expected), default=0)
    if "categorical" in expected and field.categorical:
        count = field.non_null_count
    if "identity" in expected and field.identity.strength == "candidate":
        count = field.non_null_count
    coverage = ratio(field.pattern_checked, field.non_null_count)
    blockers: set[str] = set()
    if field.pattern_skipped:
        blockers.add("PATTERN_EVIDENCE_INCOMPLETE")
    if count < field.non_null_count and field.pattern_checked == field.non_null_count:
        blockers.add("SEMANTIC_PATTERN_CONFLICT")
    return PatternMatch(
        ratio(count, field.non_null_count), True, coverage, tuple(sorted(blockers))
    )


def _numeric_overflow(field: NormalizedFieldProfile, data_type: DatabaseType) -> bool:
    ranges = {
        "smallint": (-32768, 32767),
        "int2": (-32768, 32767),
        "integer": (-2147483648, 2147483647),
        "int4": (-2147483648, 2147483647),
        "bigint": (-9223372036854775808, 9223372036854775807),
        "int8": (-9223372036854775808, 9223372036854775807),
    }
    bounds = ranges.get(data_type.native_type.casefold())
    for extrema in field.extrema:
        for value in (extrema.minimum, extrema.maximum):
            if not isinstance(value, IntegerScalar | DecimalScalar | NumberScalar):
                continue
            numeric = Decimal(str(value.value))
            if (
                data_type.canonical_type == "integer"
                and numeric != numeric.to_integral_value()
            ):
                return True
            if (
                data_type.canonical_type == "integer"
                and bounds is not None
                and (numeric < bounds[0] or numeric > bounds[1])
            ):
                return True
            if (
                data_type.canonical_type == "decimal"
                and data_type.precision is not None
                and data_type.scale is not None
                and numeric
                and numeric.copy_abs().adjusted()
                >= data_type.precision - data_type.scale
            ):
                return True
    return False


def type_compatibility(
    field: NormalizedFieldProfile,
    column: ColumnCatalog,
    *,
    dialect: str = "postgresql",
) -> Compatibility:
    """Hard mismatch никогда не компенсируется lexical score; конверсий нет."""
    blockers: set[str] = set()
    if field.inference.status != "resolved":
        blockers.add("TYPE_EVIDENCE_INCOMPLETE")
    if any(
        r in field.reasons or r in field.inference.reasons
        for r in (
            "mixed_kinds",
            "locale_ambiguity",
            "currency_ambiguity",
            "declared_type_conflict",
            "parse_issues",
            "naive_datetime",
        )
    ):
        blockers.add("SOURCE_EVIDENCE_CONFLICT")
    if not column.nullable and field.null_count:
        blockers.add("REQUIRED_VALUE_MISSING")
    if column.inspection is None or not field.non_null_count:
        return Compatibility(
            "unknown", Decimal(0), tuple(sorted(blockers | {"TYPE_UNKNOWN"}))
        )
    declared = column.inspection.data_type
    data_type = declared
    while data_type.type_kind == "domain" and data_type.base_type is not None:
        blockers.add("DOMAIN_VALIDATION_REQUIRED")
        if data_type.domain_not_null and field.null_count:
            blockers.add("REQUIRED_VALUE_MISSING")
        data_type = data_type.base_type
    if data_type.type_kind == "enum":
        return Compatibility(
            "unknown",
            Decimal(0),
            tuple(sorted(blockers | {"ENUM_VALIDATION_REQUIRED"})),
        )
    if data_type.type_kind in {
        "array",
        "unknown",
        "domain",
    } or data_type.canonical_type in {
        "unknown",
        "json",
        "time",
    }:
        return Compatibility(
            "unknown", Decimal(0), tuple(sorted(blockers | {"TYPE_UNKNOWN"}))
        )
    target = data_type.canonical_type
    if target == "text" and data_type.length is not None and field.max_length is None:
        blockers.add("LENGTH_VALIDATION_REQUIRED")
    observed = {k.name for k in field.observed_kinds if k.count and k.name != "null"}
    if (
        data_type.length is not None
        and field.max_length is not None
        and field.max_length > data_type.length
        and target == "text"
    ):
        return Compatibility("incompatible", Decimal(0), ("LENGTH_OVERFLOW",))
    # SQLite INTEGER имеет 64-bit storage; PostgreSQL INTEGER — int4.
    if dialect == "sqlite" and target == "integer":
        data_type = data_type.model_copy(update={"native_type": "bigint"})
    if _numeric_overflow(field, data_type):
        return Compatibility("incompatible", Decimal(0), ("NUMERIC_OVERFLOW",))
    if data_type.precision is not None or data_type.scale is not None:
        blockers.add("PRECISION_VALIDATION_REQUIRED")
    if target == "datetime" and data_type.timezone is not True:
        blockers.add("TIMEZONE_VALIDATION_REQUIRED")
    identity = field.identity.strength in {"candidate", "possible_identifier"}
    identity = identity or field.inference.inferred_type == "identifier"
    identity = identity or field.declared_semantic_type in {
        "identifier",
        "inn",
        "phone",
    }
    identity = identity or any(
        p.count and p.code in {"phone", "russian_inn_10", "russian_inn_12"}
        for p in field.patterns
    )
    if target in {"integer", "decimal", "float"} and "string" in observed and identity:
        return Compatibility("incompatible", Decimal(0), ("IDENTIFIER_FORMAT_LOSS",))
    native: dict[str, set[str]] = {
        "text": {"string"},
        "integer": {"integer"},
        "decimal": {"integer", "decimal"},
        "float": {"number"},
        "boolean": {"boolean"},
        "date": {"date"},
        "datetime": {"datetime"},
    }
    if observed and observed <= native.get(target, set()):
        return Compatibility("compatible", Decimal(1), tuple(sorted(blockers)))
    convertible = {
        "string": {
            "text",
            "integer",
            "decimal",
            "float",
            "boolean",
            "date",
            "datetime",
            "uuid",
        },
        "boolean": {"boolean"},
        "integer": {"integer", "decimal", "float"},
        "decimal": {"integer", "decimal", "float"},
        "number": {"integer", "decimal", "float"},
        "date": {"date", "datetime"},
        "datetime": {"date", "datetime"},
    }
    if any(target not in convertible.get(kind, set()) for kind in observed):
        return Compatibility("incompatible", Decimal(0), ("TYPE_INCOMPATIBLE",))
    counts: dict[str, int] = {p.code: p.count for p in field.patterns}
    string_targets = {
        "integer": ("integer",),
        "decimal": ("decimal", "money", "integer"),
        "float": ("decimal", "integer"),
        "boolean": ("boolean",),
        "date": ("date",),
        "datetime": ("datetime",),
        "uuid": ("uuid",),
    }
    if observed == {"string"} and target in string_targets:
        count = max((counts.get(p, 0) for p in string_targets[target]), default=0)
        if count == field.non_null_count and field.inference.status == "resolved":
            return Compatibility(
                "conditional",
                Decimal("0.75"),
                tuple(sorted(blockers | {"TRANSFORMATION_REQUIRED"})),
            )
        if count or field.pattern_skipped:
            return Compatibility(
                "unknown", Decimal(0), tuple(sorted(blockers | {"TYPE_UNKNOWN"}))
            )
        return Compatibility("incompatible", Decimal(0), ("TYPE_INCOMPATIBLE",))
    if observed <= {"integer", "decimal", "number"} and target in {
        "integer",
        "decimal",
        "float",
    }:
        return Compatibility(
            "conditional",
            Decimal("0.50"),
            tuple(sorted(blockers | {"LOSSY_CONVERSION"})),
        )
    if observed <= {"date", "datetime"} and target in {"date", "datetime"}:
        return Compatibility(
            "conditional",
            Decimal("0.50"),
            tuple(sorted(blockers | {"TRANSFORMATION_REQUIRED"})),
        )
    if len(observed) > 1:
        return Compatibility(
            "unknown", Decimal(0), tuple(sorted(blockers | {"MIXED_KINDS"}))
        )
    return Compatibility("incompatible", Decimal(0), ("TYPE_INCOMPATIBLE",))
