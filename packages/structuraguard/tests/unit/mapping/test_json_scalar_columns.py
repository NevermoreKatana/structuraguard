"""JSON-скаляры проходят одинаковые проверки mapper, validator и writer."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_mock_engine
from tests.fakes.mapping import column, table
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import (
    BooleanScalar,
    DateScalar,
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    NumberScalar,
    StringScalar,
)
from structuraguard.contracts.database import ColumnCatalog
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.database._constraint_queries import _relation
from structuraguard.domain.constraint_values import field_codes
from structuraguard.mapping._compatibility import type_compatibility
from structuraguard.mapping._validation_report import Issues
from structuraguard.mapping._validation_types import check_type
from structuraguard.profiling import NormalizedDataProfiler


def json_column(native: str = "jsonb", *, nullable: bool = True) -> ColumnCatalog:
    target = column("CTX", "json")
    assert target.inspection is not None
    return target.model_copy(
        update={
            "nullable": nullable,
            "inspection": target.inspection.model_copy(
                update={
                    "data_type": target.inspection.data_type.model_copy(
                        update={
                            "native_type": native,
                            "type_kind": "builtin",
                        }
                    ),
                }
            ),
        }
    )


@pytest.mark.anyio
@pytest.mark.parametrize("native", ["json", "jsonb"])
@pytest.mark.parametrize(
    "values",
    [
        [StringScalar(value="{'context': Customer(id=1)}")] * 3,
        [StringScalar(value="00123")],
        [NullScalar()] * 3,
        [
            StringScalar(value='{"a":1}'),
            IntegerScalar(value=7),
            BooleanScalar(value=True),
            NumberScalar(value=1.25),
            NullScalar(),
        ],
    ],
)
async def test_json_native_scalars_are_compatible_without_parsing_text(
    native: str,
    values: list[NormalizedScalar],
) -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"context": value} for value in values])
    )
    target = json_column(native)
    field = profile.fields[0]
    evidence = type_compatibility(field, target)
    assert evidence.status == "compatible"
    assert evidence.blockers == ()
    issues = Issues(MappingValidationOptions())
    check_type(
        target, "unresolved", field, "postgresql", MappingIssueLocation(), issues
    )
    assert not issues.entries
    for value in values:
        assert field_codes(target, value, "postgresql") == ()


@pytest.mark.parametrize(
    "value",
    [
        DecimalScalar(value=Decimal("1.234567890123456789")),
        DateScalar(value=date(2026, 1, 1)),
    ],
)
def test_json_never_implicitly_stringifies_non_json_types(
    value: NormalizedScalar,
) -> None:
    assert field_codes(json_column(), value, "postgresql")


@pytest.mark.anyio
async def test_json_not_null_and_uninspected_types_still_block() -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"context": NullScalar()}])
    )
    target = json_column(nullable=False)
    assert (
        "REQUIRED_VALUE_MISSING"
        in type_compatibility(profile.fields[0], target).blockers
    )
    assert field_codes(target, NullScalar(), "postgresql") == ("DB_NOT_NULL",)
    for value in ("nul\x00", "surrogate\ud800"):
        assert "DB_VALUE_INVALID" in field_codes(
            target, StringScalar(value=value), "postgresql"
        )
    assert field_codes(
        json_column("custom_json"), StringScalar(value="text"), "postgresql"
    )
    assert field_codes(json_column(), StringScalar(value="text"), "sqlite")


@pytest.mark.parametrize("native", ["json", "jsonb"])
def test_json_binding_preserves_strings_and_sql_null(native: str) -> None:
    relation = _relation(table("logs", json_column(native)), ("CTX",))
    kind = relation.c.CTX.type
    assert str(kind) == native.upper()
    dialect = create_mock_engine("postgresql://", lambda *args, **kwargs: None).dialect
    serialize = kind.bind_processor(dialect)
    assert serialize is not None
    assert serialize("{'context': Customer(id=1)}") == "\"{'context': Customer(id=1)}\""
    assert serialize('{"a":1}') == '"{\\"a\\":1}"'
    assert serialize(None) is None
