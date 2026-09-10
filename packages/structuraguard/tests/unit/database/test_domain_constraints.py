"""Domain CHECK descriptors: совместимость DTO и canonical fingerprint."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from structuraguard.contracts import (
    ConstraintInspectionMetadata,
    DatabaseMetadataSnapshot,
    DatabaseType,
)
from structuraguard.domain import database_fingerprint


def domain(checks: tuple[ConstraintInspectionMetadata, ...]) -> DatabaseType:
    return DatabaseType(
        native_type="app.positive",
        canonical_type="integer",
        type_kind="domain",
        domain_checks=tuple(c.expression for c in checks if c.expression is not None),
        domain_constraints=checks,
    )


def snapshot_with_type(
    snapshot: DatabaseMetadataSnapshot, data_type: DatabaseType
) -> DatabaseMetadataSnapshot:
    schema = snapshot.schemas[0]
    table = schema.tables[0]
    column = table.columns[0]
    assert column.inspection is not None
    changed = column.model_copy(
        update={
            "type_name": data_type.canonical_type,
            "inspection": column.inspection.model_copy(update={"data_type": data_type}),
        }
    )
    table = table.model_copy(update={"columns": (changed, *table.columns[1:])})
    return snapshot.model_copy(
        update={
            "dialect": "postgresql",
            "schemas": (
                schema.model_copy(update={"tables": (table,)}),
                *snapshot.schemas[1:],
            ),
        }
    )


def test_domain_descriptor_is_optional_and_round_trips() -> None:
    legacy = DatabaseType(native_type="integer", canonical_type="integer")
    assert "domain_constraints" not in legacy.model_dump()
    current = domain(
        (
            ConstraintInspectionMetadata(
                name="positive",
                kind="check",
                expression="VALUE > 0",
                comment=" Строка\n ",
                validated=False,
            ),
        )
    )
    assert DatabaseType.model_validate_json(current.model_dump_json()) == current
    legacy_domain = DatabaseType.model_validate(
        current.model_dump(exclude={"domain_constraints"})
    )
    assert legacy_domain.domain_checks == current.domain_checks
    assert legacy_domain.domain_constraints is None
    assert "domain_constraints" not in legacy_domain.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"domain_checks": ()},
        {"type_kind": "builtin"},
        {"domain_constraints": []},
        {
            "domain_constraints": [
                {"name": "positive", "kind": "unique", "expression": "VALUE > 0"}
            ]
        },
        {
            "domain_constraints": [
                {
                    "name": "positive",
                    "kind": "check",
                    "column_ids": ["id"],
                    "expression": "VALUE > 0",
                }
            ]
        },
        {"domain_constraints": [{"name": "positive", "kind": "check"}]},
    ],
)
def test_domain_descriptors_reject_inconsistent_checks(
    changes: dict[str, object],
) -> None:
    original = domain(
        (
            ConstraintInspectionMetadata(
                name="positive", kind="check", expression="VALUE > 0"
            ),
        )
    )
    with pytest.raises(ValidationError):
        DatabaseType.model_validate({**original.model_dump(), **changes})


@pytest.mark.parametrize("wrapper", ["direct", "base", "array"])
@pytest.mark.parametrize(
    "changes", [{"name": "renamed"}, {"comment": "Другой смысл"}, {"validated": False}]
)
def test_domain_metadata_changes_nested_fingerprint(
    catalog_snapshot: DatabaseMetadataSnapshot, wrapper: str, changes: dict[str, object]
) -> None:
    check = ConstraintInspectionMetadata(
        name="positive", kind="check", expression="VALUE > 0"
    )
    original = domain((check,))
    changed = domain((check.model_copy(update=changes),))
    if wrapper == "base":
        original = DatabaseType(
            native_type="app.outer_domain",
            canonical_type="integer",
            type_kind="domain",
            base_type=original,
        )
        changed = original.model_copy(update={"base_type": changed})
    elif wrapper == "array":
        original = DatabaseType(
            native_type="app.positive[]",
            canonical_type="unknown",
            type_kind="array",
            element_type=original,
        )
        changed = original.model_copy(update={"element_type": changed})
    assert database_fingerprint(
        snapshot_with_type(catalog_snapshot, original)
    ) != database_fingerprint(snapshot_with_type(catalog_snapshot, changed))


def test_domain_check_order_does_not_change_fingerprint(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    checks = (
        ConstraintInspectionMetadata(
            name="positive", kind="check", expression="VALUE > 0"
        ),
        ConstraintInspectionMetadata(
            name="also_positive", kind="check", expression="VALUE > 0", validated=False
        ),
        ConstraintInspectionMetadata(
            name="bounded",
            kind="check",
            expression="VALUE < 100",
            comment="Верхняя граница",
        ),
    )
    expected = database_fingerprint(
        snapshot_with_type(catalog_snapshot, domain(checks))
    )

    @given(st.permutations(checks))
    def check_order(order: list[ConstraintInspectionMetadata]) -> None:
        assert (
            database_fingerprint(
                snapshot_with_type(catalog_snapshot, domain(tuple(order)))
            )
            == expected
        )

    check_order()
    duplicate = domain(checks).model_dump()
    duplicate["domain_constraints"][1]["name"] = "positive"
    with pytest.raises(ValidationError):
        DatabaseType.model_validate(duplicate)
