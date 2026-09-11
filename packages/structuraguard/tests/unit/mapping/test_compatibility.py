"""False matches, locale и ограничения DB types по полным агрегатам."""

from decimal import Decimal

import pytest
from tests.fakes.mapping import column, profile
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import (
    DecimalScalar,
    IntegerScalar,
    NullScalar,
    StringScalar,
)
from structuraguard.contracts.database import ColumnInspectionMetadata, DatabaseType
from structuraguard.contracts.profiling import LocalePolicy, NormalizedProfilingOptions
from structuraguard.mapping._compatibility import pattern_match, type_compatibility
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("text", "kind", "status"),
    [
        ("not a date", "date", "incompatible"),
        ("person@example.org", "integer", "incompatible"),
        ("7707083893", "integer", "incompatible"),
        ("000123", "text", "compatible"),
        ("2026-09-01", "date", "conditional"),
        ("550e8400-e29b-41d4-a716-446655440000", "uuid", "conditional"),
    ],
)
async def test_value_type_mismatch_has_no_name_override(
    text: str, kind: str, status: str
) -> None:
    f = (
        await profile(
            {"id" if text == "7707083893" else "value": StringScalar(value=text)}
        )
    ).fields[0]
    result = type_compatibility(f, column(f.field.field_name, kind))
    assert result.status == status
    if status == "conditional":
        assert "TRANSFORMATION_REQUIRED" in result.blockers


@pytest.mark.parametrize(
    ("locale", "text"),
    [(LocalePolicy.RU_RU, "125 000,50"), (LocalePolicy.EN_US, "125,000.50")],
)
async def test_numeric_locale_evidence_remains_conditional(
    locale: LocalePolicy, text: str
) -> None:
    data = await NormalizedDataProfiler(
        NormalizedProfilingOptions(locale=locale)
    ).profile(normalized_stream([{"amount": StringScalar(value=text)}] * 25))
    result = type_compatibility(data.fields[0], column("amount", "decimal"))
    assert result.status == "conditional"
    assert result.score == Decimal("0.75")


async def test_native_patterns_and_null_only_are_not_fabricated() -> None:
    field = (await profile({"count": IntegerScalar(value=12)})).fields[0]
    assert pattern_match(field, ("integer",)).score == 1
    null = (await profile({"count": NullScalar()})).fields[0]
    assert type_compatibility(null, column("count", "integer")).status == "unknown"
    assert not pattern_match(null, ("integer",)).available


async def test_length_and_precision_overflow_are_hard_conflicts() -> None:
    data = await profile({"name": StringScalar(value="too long")})
    target = column("name").model_copy(
        update={
            "inspection": ColumnInspectionMetadata(
                ordinal_position=0,
                data_type=DatabaseType(
                    native_type="VARCHAR(2)", canonical_type="text", length=2
                ),
            )
        }
    )
    assert type_compatibility(data.fields[0], target).status == "incompatible"
    data = await profile({"price": DecimalScalar(value=Decimal("1234.50"))})
    target = column("price", "decimal").model_copy(
        update={
            "inspection": ColumnInspectionMetadata(
                ordinal_position=0,
                data_type=DatabaseType(
                    native_type="NUMERIC(4,2)",
                    canonical_type="decimal",
                    precision=4,
                    scale=2,
                ),
            )
        }
    )
    assert type_compatibility(data.fields[0], target).status == "incompatible"


async def test_enum_and_domain_checks_require_further_validation() -> None:
    data = await profile({"status": StringScalar(value="active")})
    for data_type in (
        DatabaseType(
            native_type="state",
            canonical_type="text",
            type_kind="enum",
            enum_labels=("active", "closed"),
        ),
        DatabaseType(
            native_type="label",
            canonical_type="text",
            type_kind="domain",
            base_type=DatabaseType(native_type="TEXT", canonical_type="text"),
            domain_checks=("value <> 'active'",),
        ),
    ):
        target = column("status").model_copy(
            update={
                "inspection": ColumnInspectionMetadata(
                    ordinal_position=0, data_type=data_type
                )
            }
        )
        result = type_compatibility(data.fields[0], target)
        assert result.blockers


async def test_fractional_native_number_cannot_target_integer() -> None:
    data = await profile({"amount": DecimalScalar(value=Decimal("1.25"))})
    assert (
        type_compatibility(data.fields[0], column("amount", "integer")).status
        == "incompatible"
    )


async def test_partial_semantic_pattern_never_looks_fully_valid() -> None:
    data = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"email": StringScalar(value="person@example.org")}] * 24
            + [{"email": StringScalar(value="invalid")}]
        )
    )
    evidence = pattern_match(data.fields[0], ("email",))
    assert evidence.score == Decimal("0.96")
    assert "SEMANTIC_PATTERN_CONFLICT" in evidence.blockers
