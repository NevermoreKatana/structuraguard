"""Temporal typmod не получает pass без моделирования точности target."""

from datetime import UTC, datetime

import pytest
from tests.fakes.mapping import column

from structuraguard.contracts.common import DateTimeScalar
from structuraguard.contracts.database import ColumnCatalog
from structuraguard.database.normalization import normalize_type
from structuraguard.domain.constraint_values import field_codes


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("timestamp(0) with time zone", ("DB_CONSTRAINT_UNVERIFIED",)),
        ("timestamp(3) with time zone", ("DB_CONSTRAINT_UNVERIFIED",)),
        ("timestamp(6) with time zone", ("DB_CONSTRAINT_UNVERIFIED",)),
        ("timestamp with time zone", ()),
    ],
)
def test_timestamp_typmod_cannot_silently_round_values(
    native: str, expected: tuple[str, ...]
) -> None:
    payload = column("at", "datetime").model_dump()
    payload["inspection"]["data_type"] = normalize_type(native).model_dump()
    value = DateTimeScalar(value=datetime(2026, 9, 13, microsecond=123456, tzinfo=UTC))
    assert (
        field_codes(ColumnCatalog.model_validate(payload), value, "postgresql")
        == expected
    )
    assert value.value.microsecond == 123456
