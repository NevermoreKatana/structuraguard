from __future__ import annotations

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    ColumnCatalog,
    ColumnInspectionMetadata,
    DatabaseMetadataSnapshot,
    DatabaseType,
    IndexKeyCatalog,
    TableCatalog,
    TableInspectionMetadata,
)


def test_legacy_column_wire_format_does_not_gain_empty_metadata() -> None:
    column = ColumnCatalog(
        column_id="table.id",
        name="id",
        type_name="integer",
        nullable=False,
    )
    expected = {
        "column_id": "table.id",
        "name": "id",
        "type_name": "integer",
        "nullable": False,
        "primary_key": False,
        "unique": False,
        "generated": False,
        "writable": True,
        "comment": None,
    }
    assert column.model_dump(mode="json") == expected
    assert ColumnCatalog.model_validate_json(column.model_dump_json()) == column
    table = TableCatalog(
        table_id="table", schema_name="main", name="table", columns=(column,)
    )
    assert "inspection" not in table.model_dump()


def test_column_metadata_cannot_disagree_with_legacy_flags() -> None:
    metadata = ColumnInspectionMetadata(
        data_type=DatabaseType(native_type="INTEGER", canonical_type="integer"),
        ordinal_position=0,
        generation_expression="1 + 1",
        generation_storage="stored",
    )
    with pytest.raises(ValidationError):
        ColumnCatalog(
            column_id="table.id",
            name="id",
            type_name="text",
            nullable=False,
            generated=True,
            writable=False,
            inspection=metadata,
        )
    with pytest.raises(ValidationError):
        ColumnCatalog(
            column_id="table.id",
            name="id",
            type_name="integer",
            nullable=False,
            inspection=metadata,
        )


def test_index_key_does_not_accept_ambiguous_or_empty_target() -> None:
    with pytest.raises(ValidationError):
        IndexKeyCatalog()
    with pytest.raises(ValidationError):
        IndexKeyCatalog(column_id="x", expression="x+1")


def test_view_cannot_be_writable() -> None:
    column = ColumnCatalog(
        column_id="id", name="id", type_name="integer", nullable=True
    )
    with pytest.raises(ValidationError):
        TableCatalog(
            table_id="v",
            schema_name="main",
            name="v",
            columns=(column,),
            inspection=TableInspectionMetadata(kind="view"),
        )


def test_metadata_snapshot_does_not_accept_unknown_version() -> None:
    with pytest.raises(ValidationError):
        DatabaseMetadataSnapshot.model_validate(
            {
                "schema_version": "9.0.0",
                "dialect": "sqlite",
                "target_id": "test",
                "target_policy_fingerprint": "sha256:" + "a" * 64,
                "schemas": [],
                "comments_supported": False,
            }
        )


def test_identity_always_cannot_be_writable() -> None:
    with pytest.raises(ValidationError):
        ColumnCatalog(
            column_id="id",
            name="id",
            type_name="integer",
            nullable=False,
            inspection=ColumnInspectionMetadata(
                data_type=DatabaseType(native_type="bigint", canonical_type="integer"),
                ordinal_position=0,
                identity="always",
            ),
        )


def test_comments_preserve_formatting_but_reject_secrets() -> None:
    metadata = ColumnInspectionMetadata(
        data_type=DatabaseType(native_type="integer", canonical_type="integer"),
        ordinal_position=0,
    )
    column = ColumnCatalog(
        column_id="id",
        name="id",
        type_name="integer",
        nullable=True,
        comment=" line 1\nline 2\t ",
        inspection=metadata,
    )
    assert column.comment == " line 1\nline 2\t "
    with pytest.raises(ValidationError):
        ColumnCatalog(
            column_id="id",
            name="id",
            type_name="integer",
            nullable=True,
            comment="line 1\npassword=canary",
            inspection=metadata,
        )
