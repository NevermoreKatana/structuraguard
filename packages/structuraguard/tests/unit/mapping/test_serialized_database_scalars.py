"""JSON DATE/UUID: точное связывание с БД, без locale cast и потери ключей."""

from datetime import date

import pytest
from tests.fakes.mapping import catalog, column, table
from tests.fakes.profiling import normalized_stream
from tests.unit.validation.test_db_constraints import data, policy

from structuraguard.contracts.common import DateScalar, StringScalar
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.domain.constraint_values import database_scalar, field_codes
from structuraguard.mapping._compatibility import type_compatibility
from structuraguard.mapping._validation_report import Issues
from structuraguard.mapping._validation_types import check_type
from structuraguard.profiling import NormalizedDataProfiler
from structuraguard.validation import DatabaseConstraintValidator


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "text"),
    [("date", "2026-09-23"), ("uuid", "00000000-0000-4000-8000-000000000001")],
)
async def test_serialized_scalars_reach_per_record_validation(
    kind: str, text: str
) -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"value": StringScalar(value=text)}] * 3)
    )
    field = profile.fields[0]
    target = column("value", kind)
    evidence = type_compatibility(field, target)
    assert evidence.status == "compatible"
    assert evidence.blockers == ()
    issues = Issues(MappingValidationOptions())
    check_type(
        target, "unresolved", field, "postgresql", MappingIssueLocation(), issues
    )
    assert not issues.entries
    assert field_codes(target, StringScalar(value=text), "postgresql") == ()


@pytest.mark.parametrize(
    "text",
    [
        "2026-02-30",
        "23.09.2026",
        "20260923",
        "2026-09-23T00:00:00Z",
        " 2026-09-23",
        "2026-W39-3",
    ],
)
def test_date_binding_never_guesses_or_truncates(text: str) -> None:
    target = column("value", "date")
    scalar = StringScalar(value=text)
    assert database_scalar(target, scalar) == scalar
    assert "DB_TYPE_MISMATCH" in field_codes(target, scalar, "postgresql")


def test_date_binding_is_exact_and_only_for_date_columns() -> None:
    value = StringScalar(value="2026-09-23")
    assert database_scalar(column("value", "date"), value) == DateScalar(
        value=date(2026, 9, 23)
    )
    assert database_scalar(column("value"), value) == value


@pytest.mark.anyio
async def test_date_string_and_native_date_collide_as_same_database_key() -> None:
    key = column("day", "date").model_copy(
        update={"nullable": False, "primary_key": True}
    )
    target = table("events", column("day", "date")).model_copy(
        update={"primary_key": ("day",), "columns": (key,)}
    )
    db = catalog(target)
    source = data(
        (target.table_id, {"day": {"kind": "string", "value": "2026-09-23"}}),
        (target.table_id, {"day": {"kind": "date", "value": "2026-09-23"}}),
    )
    report = await DatabaseConstraintValidator(policy(db)).validate(source, catalog=db)
    assert {i.record_id for i in report.issues if i.code == "DB_UNIQUE"} == {"0", "1"}


@pytest.mark.parametrize("text", ["not-a-uuid", "00000000000040008000000000000001"])
def test_uuid_values_remain_strict(text: str) -> None:
    assert "DB_VALUE_INVALID" in field_codes(
        column("value", "uuid"), StringScalar(value=text), "postgresql"
    )
