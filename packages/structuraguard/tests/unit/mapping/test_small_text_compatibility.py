"""Малые текстовые наборы не требуют семантической конверсии в TEXT."""

import pytest
from tests.fakes.mapping import column
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar, NullScalar, StringScalar
from structuraguard.contracts.database import ColumnInspectionMetadata, DatabaseType
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.mapping._compatibility import type_compatibility
from structuraguard.mapping._validation_report import Issues
from structuraguard.mapping._validation_types import check_type
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("text", ["001000", "101000", "person@example.test"])
async def test_small_complete_strings_are_compatible_with_text(text: str) -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"postal_codde": StringScalar(value=text)}] * 3)
    )
    field = profile.fields[0]
    assert field.inference.status == "insufficient_evidence"
    compatibility = type_compatibility(field, column("postal_code"))
    assert compatibility.status == "compatible"
    assert compatibility.blockers == ()


async def test_small_strings_do_not_bypass_conversion_or_column_constraints() -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"postal_codde": StringScalar(value="101000")}] * 3)
    )
    field = profile.fields[0]
    number = type_compatibility(field, column("postal_code", "integer"))
    assert number.status != "compatible"
    assert "TYPE_EVIDENCE_INCOMPLETE" in number.blockers
    limited = column("postal_code").model_copy(
        update={
            "inspection": ColumnInspectionMetadata(
                ordinal_position=0,
                data_type=DatabaseType(
                    native_type="varchar(3)", canonical_type="text", length=3
                ),
            )
        }
    )
    assert type_compatibility(field, limited).blockers == ("LENGTH_OVERFLOW",)


async def test_small_text_preserves_null_and_mixed_kind_blockers() -> None:
    nullable = await NormalizedDataProfiler().profile(
        normalized_stream(
            [
                {"postal_codde": StringScalar(value="101000")},
                {"postal_codde": NullScalar()},
            ]
        )
    )
    required = column("postal_code").model_copy(update={"nullable": False})
    result = type_compatibility(nullable.fields[0], required)
    assert "REQUIRED_VALUE_MISSING" in result.blockers
    mixed = await NormalizedDataProfiler().profile(
        normalized_stream(
            [
                {"postal_codde": StringScalar(value="101000")},
                {"postal_codde": IntegerScalar(value=101000)},
            ]
        )
    )
    assert (
        type_compatibility(mixed.fields[0], column("postal_code")).status
        != "compatible"
    )


async def test_complete_explicit_nulls_are_allowed_only_for_nullable_text() -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"notes": NullScalar()}] * 3)
    )
    field = profile.fields[0]
    target = column("notes")
    assert type_compatibility(field, target).status == "compatible"
    assert type_compatibility(field, target).blockers == ()
    issues = Issues(MappingValidationOptions())
    check_type(
        target, "unresolved", field, "postgresql", MappingIssueLocation(), issues
    )
    assert issues.ordered()[0] == ()
    required = target.model_copy(update={"nullable": False})
    assert "REQUIRED_VALUE_MISSING" in type_compatibility(field, required).blockers
    assert type_compatibility(field, column("notes", "integer")).status == "unknown"
