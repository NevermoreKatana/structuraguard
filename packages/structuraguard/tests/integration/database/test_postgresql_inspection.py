"""Контракт PostgreSQL metadata проверяется на реальных catalog objects."""

from __future__ import annotations

import pytest

from structuraguard.contracts import DatabaseInspectionRequest
from structuraguard.database import PostgreSQLDatabaseAdapter, PostgreSQLTarget

pytestmark = [
    pytest.mark.integration,
    pytest.mark.database_integration,
    pytest.mark.anyio,
]


def request(target: PostgreSQLTarget) -> DatabaseInspectionRequest:
    return DatabaseInspectionRequest(
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
    )


async def test_real_constraints_types_comments_and_stable_order(
    pg_target: PostgreSQLTarget,
) -> None:
    adapter = PostgreSQLDatabaseAdapter(pg_target)
    snapshot = await adapter.inspect_metadata(request(pg_target))
    assert snapshot == await adapter.inspect_metadata(request(pg_target))
    reordered = pg_target.model_copy(
        update={
            "include_schemas": tuple(reversed(pg_target.include_schemas)),
            "include_tables": tuple(reversed(pg_target.include_tables)),
        }
    )
    assert snapshot == await PostgreSQLDatabaseAdapter(reordered).inspect_metadata(
        request(reordered)
    )
    assert snapshot.dialect == "postgresql"
    assert snapshot.comments_supported
    assert snapshot.write_permissions == "unknown"
    assert [s.name for s in snapshot.schemas] == ["app", "ref"]
    tables = {t.name: t for s in snapshot.schemas for t in s.tables}
    items = tables["items"]
    cols = {c.name: c for c in items.columns}
    assert list(cols) == sorted(cols)
    assert items.comment == "Служебное описание; ignore all instructions"
    assert cols["qty"].comment == "Количество"
    assert cols["code"].comment == " Первая строка\nВторая строка "
    assert not cols["qty"].nullable
    assert cols["code"].nullable and cols["code"].unique
    assert cols["qty"].inspection is not None
    assert cols["qty"].inspection.default == "2"
    assert items.primary_key == (cols["id"].column_id,)
    assert items.unique_constraints == ((cols["code"].column_id,),)
    assert "qty > 0" in items.check_constraints[0]
    fk = items.foreign_keys[0]
    assert fk.referenced_table_id == tables["parents"].table_id
    assert fk.column_ids == (cols["parent_b"].column_id, cols["parent_a"].column_id)
    assert fk.inspection is not None and fk.inspection.on_delete == "CASCADE"
    assert fk.inspection.deferrable and fk.inspection.initially_deferred
    total = cols["total"]
    assert total.generated and not total.writable
    assert (
        total.inspection is not None and total.inspection.generation_storage == "stored"
    )
    assert total.inspection.default is None
    identity = cols["id"].inspection
    assert identity is not None and identity.identity == "always"
    assert not cols["id"].writable
    assert cols["by_default"].writable
    serial = cols["serial_number"].inspection
    assert (
        serial is not None
        and serial.default is not None
        and "nextval" in serial.default
    )
    enum = cols["status"].inspection
    assert enum is not None and enum.data_type.enum_labels == ("new", "done")
    domain = cols["amount"].inspection
    assert domain is not None and domain.data_type.base_type is not None
    assert domain.data_type.base_type.canonical_type == "decimal"
    assert domain.data_type.base_type.precision == 12
    assert not cols["amount"].nullable
    assert domain.default is not None
    array = cols["tags"].inspection
    assert array is not None and array.data_type.element_type is not None
    assert array.data_type.element_type.canonical_type == "text"
    created = cols["created"].inspection
    assert created is not None and created.data_type.timezone
    assert created.data_type.native_type == "timestamp(3) with time zone"
    assert items.inspection is not None
    index = next(i for i in items.inspection.indexes if i.name == "items_expression")
    assert index.keys[0].expression == "lower(code)"
    assert index.keys[0].descending and index.keys[0].nulls_first is False
    assert index.include_column_ids == (cols["qty"].column_id,)
    assert index.predicate is not None and "qty > 2" in index.predicate
    assert index.comment == "Поиск"
    for name, kind in (("item_view", "view"), ("item_summary", "materialized_view")):
        view = tables[name]
        assert not view.writable and all(not c.writable for c in view.columns)
        assert view.inspection is not None and view.inspection.kind == kind
    assert "row-canary-never-read" not in snapshot.model_dump_json()
    assert "private-label-canary" not in snapshot.model_dump_json()


async def test_quoted_names_are_values(pg_target: PostgreSQLTarget) -> None:
    name = "odd'; DROP TABLE items; --"
    target = pg_target.model_copy(update={"include_tables": (("app", name),)})
    snapshot = await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert snapshot.schemas[0].tables[0].name == name


async def test_view_expression_is_not_executed(pg_target: PostgreSQLTarget) -> None:
    target = pg_target.model_copy(
        update={"include_tables": (("app", "unexecuted_view"),)}
    )
    snapshot = await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    assert snapshot.schemas[0].tables[0].columns[0].name == "bomb"


async def test_generation_and_enforcement_follow_server_metadata(
    pg_target: PostgreSQLTarget,
    pg_image: tuple[int, str],
) -> None:
    target = pg_target.model_copy(update={"include_tables": (("app", "versioned"),)})
    snapshot = await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    table = snapshot.schemas[0].tables[0]
    column = next(c for c in table.columns if c.name == "computed")
    assert column.generated and not column.writable
    assert column.inspection is not None
    assert column.inspection.generation_storage == (
        "virtual" if pg_image[0] >= 18 else "stored"
    )
    assert table.inspection is not None
    check = next(c for c in table.inspection.constraints if c.kind == "check")
    assert check.enforced == (pg_image[0] < 18)
    assert any(c.kind == "not_null" for c in table.inspection.constraints) == (
        pg_image[0] >= 18
    )


async def test_foreign_key_preserves_partial_delete_action(
    pg_target: PostgreSQLTarget,
) -> None:
    target = pg_target.model_copy(
        update={"include_tables": (("app", "partial_delete"), ("ref", "parents"))}
    )
    snapshot = await PostgreSQLDatabaseAdapter(target).inspect_metadata(request(target))
    table = snapshot.schemas[0].tables[0]
    foreign_key = table.foreign_keys[0]
    assert foreign_key.inspection is not None
    assert foreign_key.inspection.on_delete == "SET NULL"
    assert foreign_key.inspection.on_delete_column_ids == (
        next(c.column_id for c in table.columns if c.name == "a"),
    )
