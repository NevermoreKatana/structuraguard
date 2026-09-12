"""Строгая совместимость представлений; ни cast, ни SQL CHECK не выполняются."""

from decimal import Decimal

from structuraguard.contracts.common import DecimalScalar, IntegerScalar, NumberScalar
from structuraguard.contracts.database import ColumnCatalog
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.profiling import NormalizedFieldProfile

from ._compatibility import type_compatibility
from ._validation_report import Issues


def _requires_rounding(
    profile: NormalizedFieldProfile, scale: int, issues: Issues
) -> bool:
    """Проверить точные extrema без округления и зависимости от Decimal context."""
    for extrema in profile.extrema:
        for value in (extrema.minimum, extrema.maximum):
            if not isinstance(value, IntegerScalar | DecimalScalar):
                continue
            parts = Decimal(value.value).as_tuple()
            issues.work(len(parts.digits) + 1)
            if not isinstance(parts.exponent, int) or not any(parts.digits):
                continue
            trailing_zeroes = next(
                i for i, digit in enumerate(reversed(parts.digits)) if digit
            )
            if parts.exponent + trailing_zeroes < -scale:
                return True
    return False


def check_type(
    column: ColumnCatalog,
    semantic_type: str,
    profile: NormalizedFieldProfile | None,
    dialect: str,
    location: MappingIssueLocation,
    issues: Issues,
) -> None:
    meta = column.inspection
    if meta is None:
        issues.add("MAPPING_TYPE_UNVERIFIED", location)
        return
    data_type = meta.data_type
    domain_nullable = True
    while data_type.type_kind == "domain" and data_type.base_type is not None:
        issues.work()
        domain_nullable = domain_nullable and not data_type.domain_not_null
        data_type = data_type.base_type
    target = data_type.canonical_type
    if profile is not None:
        if profile.null_count and (not column.nullable or not domain_nullable):
            issues.add("MAPPING_NULLABILITY_CONFLICT", location)
        evidence = type_compatibility(profile, column, dialect=dialect)
        for blocker in evidence.blockers:
            if blocker == "LENGTH_OVERFLOW":
                issues.add("MAPPING_LENGTH_OVERFLOW", location)
            elif blocker == "NUMERIC_OVERFLOW":
                issues.add("MAPPING_NUMERIC_OVERFLOW", location)
            elif blocker == "IDENTIFIER_FORMAT_LOSS":
                issues.add("MAPPING_TYPE_INCOMPATIBLE", location)
        if (
            dialect == "postgresql"
            and target == "float"
            and data_type.native_type.casefold() in {"real", "float4"}
        ):
            # NumberScalar хранит binary64, PostgreSQL real — конечный binary32.
            limit = float.fromhex("0x1.fffffep+127")
            for extrema in profile.extrema:
                issues.work(2)
                if any(
                    isinstance(value, NumberScalar)
                    and not -limit <= value.value <= limit
                    for value in (extrema.minimum, extrema.maximum)
                ):
                    issues.add("MAPPING_NUMERIC_OVERFLOW", location)
                    break
        if (
            target == "decimal"
            and data_type.scale is not None
            and _requires_rounding(profile, data_type.scale, issues)
        ):
            issues.add("MAPPING_TRANSFORMATION_REQUIRED", location)
        kinds = {k.name for k in profile.observed_kinds if k.count and k.name != "null"}
        if any(
            r in profile.reasons or r in profile.inference.reasons
            for r in (
                "mixed_kinds",
                "locale_ambiguity",
                "currency_ambiguity",
                "declared_type_conflict",
                "parse_issues",
                "naive_datetime",
            )
        ):
            issues.add("MAPPING_TYPE_UNVERIFIED", location)
    else:
        kinds = (
            {semantic_type}
            if semantic_type
            in {"integer", "decimal", "number", "boolean", "date", "datetime", "string"}
            else set()
        )
    if (
        not kinds
        or data_type.type_kind in {"unknown", "domain", "array"}
        or target in {"unknown", "json", "binary", "time"}
    ):
        issues.add("MAPPING_TYPE_UNVERIFIED", location)
        return
    native = {
        "integer": {"integer"},
        "decimal": {"integer", "decimal"},
        "float": {"number"},
        "boolean": {"boolean"},
        "text": {"string"},
        "date": {"date"},
        "datetime": {"datetime"},
    }
    if data_type.type_kind == "enum":
        # Семья string допустима; каждое значение enum проверяется record engine.
        if kinds != {"string"}:
            issues.add("MAPPING_TYPE_INCOMPATIBLE", location)
        return
    if target == "float" and (semantic_type == "money" or "decimal" in kinds):
        issues.add("MAPPING_TYPE_INCOMPATIBLE", location)
    elif kinds <= native.get(target, set()):
        if target == "datetime" and data_type.timezone is not True:
            issues.add("MAPPING_TRANSFORMATION_REQUIRED", location)
    elif kinds == {"string"} and target in {
        "integer",
        "decimal",
        "float",
        "boolean",
        "date",
        "datetime",
        "uuid",
    }:
        issues.add("MAPPING_TRANSFORMATION_REQUIRED", location)
    elif kinds <= {"integer", "decimal", "number"} and target in {
        "integer",
        "decimal",
        "float",
    }:
        issues.add("MAPPING_TYPE_INCOMPATIBLE", location)
    elif kinds <= {"date", "datetime"} and target in {"date", "datetime"}:
        issues.add("MAPPING_TRANSFORMATION_REQUIRED", location)
    else:
        issues.add("MAPPING_TYPE_INCOMPATIBLE", location)
