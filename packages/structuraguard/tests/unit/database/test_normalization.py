from __future__ import annotations

import pytest
from pydantic import ValidationError

from structuraguard.database import InspectionLimits
from structuraguard.database.normalization import normalize_type


@pytest.mark.parametrize(
    ("native", "canonical"),
    [
        ("INTEGER", "integer"),
        ("BIGINT", "integer"),
        ("VARCHAR(32)", "text"),
        ("NUMERIC(12, 3)", "decimal"),
        ("DOUBLE PRECISION", "float"),
        ("BOOLEAN", "boolean"),
        ("DATE", "date"),
        ("TIMESTAMP WITH TIME ZONE", "datetime"),
        ("BLOB", "binary"),
        ("vendor_custom", "unknown"),
    ],
)
def test_type_normalization_preserves_native_type(native: str, canonical: str) -> None:
    result = normalize_type(native)
    assert result.native_type == native
    assert result.canonical_type == canonical


def test_type_parameters_remain_explicit() -> None:
    numeric = normalize_type("DECIMAL(18, 4)")
    assert (numeric.precision, numeric.scale) == (18, 4)
    assert normalize_type("VARCHAR(80)").length == 80
    assert normalize_type("TIMESTAMP WITH TIME ZONE").timezone is True
    assert normalize_type("TIMESTAMP WITHOUT TIME ZONE").timezone is False


def test_sqlite_affinity_does_not_claim_declared_type_is_enforced() -> None:
    result = normalize_type("custom_point", dialect="sqlite")
    assert result.canonical_type == "unknown"
    assert result.affinity == "integer"
    assert normalize_type("", dialect="sqlite").affinity == "binary"


def test_unknown_parameterized_type_is_not_guessed() -> None:
    assert normalize_type("vendor_type(10)").canonical_type == "unknown"


def test_sqlite_sql_limit_cannot_overflow_driver_integer() -> None:
    with pytest.raises(ValidationError):
        InspectionLimits(max_sql_bytes=2**64)
