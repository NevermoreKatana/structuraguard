"""Небольшой normalized catalog для pure fingerprint/graph tests."""

from __future__ import annotations

import pytest

from structuraguard.contracts import (
    ColumnCatalog,
    ColumnInspectionMetadata,
    DatabaseMetadataSnapshot,
    DatabaseType,
    ForeignKeyCatalog,
    ForeignKeyInspectionMetadata,
    IndexCatalog,
    IndexKeyCatalog,
    SchemaCatalog,
    TableCatalog,
    TableInspectionMetadata,
)


@pytest.fixture
def catalog_snapshot() -> DatabaseMetadataSnapshot:
    def column(name: str, *, primary: bool = False) -> ColumnCatalog:
        return ColumnCatalog(
            column_id=name,
            name=name,
            type_name="integer",
            nullable=False,
            primary_key=primary,
            inspection=ColumnInspectionMetadata(
                data_type=DatabaseType(native_type="INTEGER", canonical_type="integer"),
                ordinal_position={"a": 0, "b": 1, "id": 0, "left": 1, "right": 2}[name],
            ),
        )

    parent = TableCatalog(
        table_id="parent",
        schema_name="ref",
        name="parent",
        columns=(column("a", primary=True), column("b", primary=True)),
        primary_key=("b", "a"),
        inspection=TableInspectionMetadata(),
    )
    child = TableCatalog(
        table_id="child",
        schema_name="app",
        name="child",
        columns=(column("id", primary=True), column("left"), column("right")),
        primary_key=("id",),
        unique_constraints=(("left", "right"),),
        check_constraints=("left > 0", "right > 0"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="link",
                column_ids=("right", "left"),
                referenced_table_id="parent",
                referenced_column_ids=("b", "a"),
                inspection=ForeignKeyInspectionMetadata(
                    on_update="NO ACTION", on_delete="CASCADE", match="SIMPLE"
                ),
            ),
        ),
        inspection=TableInspectionMetadata(
            indexes=(
                IndexCatalog(
                    index_id="ix1",
                    name="by_left",
                    keys=(IndexKeyCatalog(column_id="left"),),
                    unique=False,
                    origin="index",
                ),
                IndexCatalog(
                    index_id="ix2",
                    name="by_right",
                    keys=(IndexKeyCatalog(column_id="right"),),
                    unique=False,
                    origin="index",
                ),
            )
        ),
    )
    return DatabaseMetadataSnapshot(
        dialect="sqlite",
        target_id="fixture",
        target_policy_fingerprint="sha256:" + "a" * 64,
        comments_supported=False,
        schemas=(
            SchemaCatalog(schema_id="app", name="app", tables=(child,)),
            SchemaCatalog(schema_id="ref", name="ref", tables=(parent,)),
        ),
    )
