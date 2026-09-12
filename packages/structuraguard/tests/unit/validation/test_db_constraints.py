"""Детерминированные field constraints и exact composite keys."""

from collections.abc import Mapping

import pytest
from tests.fakes.mapping import catalog, column, refs, table

from structuraguard.contracts.constraint_validation import ConstraintValidationPolicy
from structuraguard.contracts.database import DatabaseCatalog, TableCatalog
from structuraguard.contracts.record_validation import ValidationDataset
from structuraguard.validation import DatabaseConstraintValidator


def policy(db: DatabaseCatalog) -> ConstraintValidationPolicy:
    return ConstraintValidationPolicy.model_validate(
        {
            "target_id": db.target_id,
            "target_policy_fingerprint": db.target_policy_fingerprint,
            "database_fingerprint": db.database_fingerprint,
            "tables": tuple(
                {"table_id": t.table_id} for s in db.schemas for t in s.tables
            ),
            "allow_columns": refs(db),
            "source_identity_allow": refs(db),
        }
    )


def data(*rows: tuple[str, Mapping[str, object]]) -> ValidationDataset:
    return ValidationDataset.model_validate(
        {
            "records": tuple(
                {
                    "record_id": str(i),
                    "collection_id": tid,
                    "values": tuple(
                        {"field_id": k, "value": v} for k, v in values.items()
                    ),
                }
                for i, (tid, values) in enumerate(rows)
            )
        }
    )


def integer(value: int) -> dict[str, object]:
    return {"kind": "integer", "value": value}


@pytest.mark.anyio
async def test_missing_null_length_scale_and_check_are_all_reported() -> None:
    cols = [
        column("name"),
        column("amount", "decimal", position=1),
        column("required", "integer", position=2),
    ]
    payload = table("items", *cols).model_dump()
    payload["columns"][0]["nullable"] = False
    payload["columns"][0]["inspection"]["data_type"].update(
        length=3, native_type="varchar"
    )
    payload["columns"][1]["inspection"]["data_type"].update(
        precision=4, scale=2, native_type="numeric"
    )
    payload["columns"][2]["nullable"] = False
    payload["check_constraints"] = ("amount > 0",)
    db = catalog(TableCatalog.model_validate(payload))
    source = data(
        (
            "public.items",
            {
                "name": {"kind": "string", "value": "long"},
                "amount": {"kind": "decimal", "value": "123.456"},
            },
        ),
        ("public.items", {"name": {"kind": "null"}, "required": integer(2**40)}),
    )
    before = source.canonical_json()
    report = await DatabaseConstraintValidator(policy(db)).validate(source, catalog=db)
    assert {i.code for i in report.issues} >= {
        "DB_NOT_NULL",
        "DB_LENGTH",
        "DB_NUMERIC_BOUNDS",
        "DB_NUMERIC_SCALE",
        "DB_CONSTRAINT_UNVERIFIED",
    }
    assert source.canonical_json() == before


@pytest.mark.anyio
async def test_composite_uniqueness_is_ordered_and_marks_every_duplicate() -> None:
    payload = table(
        "pairs", column("a", "integer"), column("b", "integer", position=1)
    ).model_dump()
    payload["unique_constraints"] = (("a", "b"),)
    db = catalog(TableCatalog.model_validate(payload))
    source = data(
        *(
            ("public.pairs", {"a": integer(a), "b": integer(b)})
            for a, b in [(1, 2), (2, 1), (1, 2)]
        )
    )
    report = await DatabaseConstraintValidator(policy(db)).validate(source, catalog=db)
    duplicates = [i.record_id for i in report.issues if i.code == "DB_UNIQUE"]
    assert set(duplicates) == {"0", "2"}
    assert "DB_CONSTRAINT_UNVERIFIED" in {i.code for i in report.issues}


@pytest.mark.anyio
async def test_enum_check_representation_and_defaults() -> None:
    from structuraguard.contracts._base import canonical_sha256_value
    from structuraguard.contracts.business_rules import BusinessRuleSet
    from structuraguard.contracts.constraint_validation import CheckRuleBinding

    payload = table(
        "items", column("n", "integer"), column("status", position=1)
    ).model_dump()
    payload["columns"][0]["nullable"] = False
    payload["columns"][0]["inspection"]["default"] = "1"
    payload["columns"][1]["inspection"]["data_type"].update(
        type_kind="enum", enum_labels=("new", "done")
    )
    payload["check_constraints"] = ("n > 0",)
    db = catalog(TableCatalog.model_validate(payload))
    spec = BusinessRuleSet.model_validate(
        {
            "fields": (
                {
                    "collection_id": "public.items",
                    "field_id": "n",
                    "value_type": "integer",
                },
            ),
            "rules": (
                {
                    "op": "gt",
                    "rule_id": "positive",
                    "collection_id": "public.items",
                    "left": {"kind": "field", "field_id": "n", "value_type": "integer"},
                    "right": {"kind": "literal", "value": integer(0)},
                    "nulls": "pass",
                },
            ),
        }
    )
    binding = CheckRuleBinding(
        table_id="public.items",
        expression_fingerprint=canonical_sha256_value("n > 0"),
        equivalence_verified=True,
        rules=spec,
    )
    configured = policy(db).model_copy(update={"checks": (binding,)})
    report = await DatabaseConstraintValidator(configured).validate(
        data(
            (
                "public.items",
                {"n": integer(-1), "status": {"kind": "string", "value": "bad"}},
            )
        ),
        catalog=db,
    )
    assert {i.code for i in report.issues} == {"DB_ENUM", "DB_CHECK"}
    missing = await DatabaseConstraintValidator(configured).validate(
        data(("public.items", {"status": {"kind": "null"}})), catalog=db
    )
    assert [i.code for i in missing.issues] == ["RULE_MISSING_OPERAND"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12300", set()),
        ("12345", {"DB_NUMERIC_SCALE"}),
        ("123456", {"DB_NUMERIC_BOUNDS", "DB_NUMERIC_SCALE"}),
    ],
)
async def test_negative_numeric_scale_never_rounds(
    value: str, expected: set[str]
) -> None:
    payload = table("n", column("amount", "decimal")).model_dump()
    payload["columns"][0]["inspection"]["data_type"].update(precision=3, scale=-2)
    db = catalog(TableCatalog.model_validate(payload))
    report = await DatabaseConstraintValidator(policy(db)).validate(
        data(("public.n", {"amount": {"kind": "decimal", "value": value}})), catalog=db
    )
    assert {i.code for i in report.issues} == expected


@pytest.mark.anyio
async def test_late_incoming_parent_resolves_composite_fk() -> None:
    from structuraguard.contracts.database import (
        ForeignKeyCatalog,
        ForeignKeyInspectionMetadata,
    )

    parent_data = table(
        "parent", column("a", "integer"), column("b", "integer", position=1)
    ).model_dump()
    parent_data["unique_constraints"] = (("b", "a"),)
    parent = TableCatalog.model_validate(parent_data)
    child = table(
        "child",
        column("x", "integer"),
        column("y", "integer", position=1),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="fk",
                column_ids=("y", "x"),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("b", "a"),
                inspection=ForeignKeyInspectionMetadata(
                    on_update="NO ACTION", on_delete="NO ACTION", match="FULL"
                ),
            ),
        ),
    )
    db = catalog(parent, child)
    report = await DatabaseConstraintValidator(policy(db)).validate(
        data(
            (child.table_id, {"x": integer(1), "y": integer(2)}),
            (parent.table_id, {"a": integer(1), "b": integer(2)}),
        ),
        catalog=db,
    )
    assert not any(i.collection_id == child.table_id for i in report.issues)
    partial_null = await DatabaseConstraintValidator(policy(db)).validate(
        data((child.table_id, {"x": {"kind": "null"}, "y": integer(2)})), catalog=db
    )
    assert [i.code for i in partial_null.issues] == ["DB_FOREIGN_KEY"]


@pytest.mark.anyio
async def test_wrong_types_and_generated_values_are_not_repaired() -> None:
    payload = table(
        "items", column("n", "integer"), column("derived", position=1)
    ).model_dump()
    payload["columns"][1].update(generated=True, writable=False)
    payload["columns"][1]["inspection"].update(
        generation_expression="n", generation_storage="stored"
    )
    db = catalog(TableCatalog.model_validate(payload))
    report = await DatabaseConstraintValidator(policy(db)).validate(
        data(
            (
                "public.items",
                {"n": {"kind": "boolean", "value": True}, "derived": {"kind": "null"}},
            )
        ),
        catalog=db,
    )
    assert {i.code for i in report.issues} == {
        "DB_TYPE_MISMATCH",
        "DB_COLUMN_NOT_WRITABLE",
    }


@pytest.mark.anyio
async def test_fk_operand_type_checked_against_parent_before_reader() -> None:
    from structuraguard.contracts.database import (
        ForeignKeyCatalog,
        ForeignKeyInspectionMetadata,
    )

    parent_data = table("parent", column("id", "integer")).model_dump()
    parent_data["unique_constraints"] = (("id",),)
    parent = TableCatalog.model_validate(parent_data)
    child = table(
        "child",
        column("parent_id"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="fk",
                column_ids=("parent_id",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("id",),
                inspection=ForeignKeyInspectionMetadata(
                    on_update="NO ACTION", on_delete="NO ACTION", match="SIMPLE"
                ),
            ),
        ),
    )
    db = catalog(parent, child)
    report = await DatabaseConstraintValidator(policy(db)).validate(
        data((child.table_id, {"parent_id": {"kind": "string", "value": "1"}})),
        catalog=db,
    )
    assert [i.code for i in report.issues] == ["DB_TYPE_MISMATCH"]


@pytest.mark.anyio
async def test_check_evaluation_budget_is_shared_across_bindings() -> None:
    from structuraguard.contracts._base import canonical_sha256_value
    from structuraguard.contracts.business_rules import BusinessRuleSet
    from structuraguard.contracts.constraint_validation import CheckRuleBinding
    from structuraguard.contracts.record_validation import RecordValidationLimits
    from structuraguard.exceptions import ValidationError

    payload = table("items", column("n", "integer")).model_dump()
    payload["check_constraints"] = ("n > 0", "n >= 1")
    db = catalog(TableCatalog.model_validate(payload))
    spec = BusinessRuleSet.model_validate(
        {
            "fields": (),
            "rules": (
                {
                    "op": "equals",
                    "rule_id": "r",
                    "collection_id": "public.items",
                    "left": {"kind": "literal", "value": integer(1)},
                    "right": {"kind": "literal", "value": integer(1)},
                },
            ),
        }
    )
    configured = policy(db).model_copy(
        update={
            "limits": RecordValidationLimits(max_evaluations=12),
            "checks": tuple(
                CheckRuleBinding(
                    table_id="public.items",
                    expression_fingerprint=canonical_sha256_value(expr),
                    equivalence_verified=True,
                    rules=spec,
                )
                for expr in payload["check_constraints"]
            ),
        }
    )
    with pytest.raises(ValidationError) as error:
        await DatabaseConstraintValidator(configured).validate(
            data(("public.items", {"n": integer(1)})), catalog=db
        )
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"
