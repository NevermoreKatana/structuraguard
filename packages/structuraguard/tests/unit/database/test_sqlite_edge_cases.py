from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from structuraguard.contracts.database import (
    DatabaseInspectionRequest,
    DatabaseMetadataSnapshot,
)
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.exceptions import DatabaseInspectionError


async def inspect_fixture(
    path: Path, names: tuple[str, ...]
) -> DatabaseMetadataSnapshot:
    target = SQLiteTarget(path=path, target_id="fixture", include_tables=names)
    return await SQLiteDatabaseAdapter(target).inspect_metadata(
        DatabaseInspectionRequest(
            target_id="fixture",
            target_policy_fingerprint=target.policy_fingerprint,
        )
    )


@pytest.mark.anyio
async def test_view_metadata_is_read_only_and_has_no_inferred_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "view.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE VIEW v AS SELECT id, name FROM t;"
        )
    snapshot = await inspect_fixture(path, ("v",))
    view = snapshot.schemas[0].tables[0]
    assert view.inspection is not None and view.inspection.kind == "view"
    assert not view.writable
    assert not any(column.writable for column in view.columns)
    assert not view.primary_key and not view.foreign_keys
    assert (
        DatabaseMetadataSnapshot.model_validate_json(snapshot.model_dump_json())
        == snapshot
    )


@pytest.mark.anyio
async def test_checks_and_generated_expression_respect_sql_literals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "expressions.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE t (a TEXT CHECK (a <> ') CHECK (') "
            "CHECK (length(a) > 1), b TEXT AS (a || ')'))"
        )
    table = (await inspect_fixture(path, ("t",))).schemas[0].tables[0]
    assert table.check_constraints == ("a <> ') CHECK ('", "length(a) > 1")
    column = next(column for column in table.columns if column.name == "b")
    assert column.inspection is not None
    assert column.inspection.generation_expression == "a || ')'"
    assert column.inspection.generation_storage == "virtual"


@pytest.mark.anyio
async def test_implicit_foreign_key_targets_actual_composite_primary_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "implicit.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE p (a TEXT NOT NULL, b INTEGER NOT NULL, PRIMARY KEY(b,a));"
            "CREATE TABLE c (x INTEGER, y TEXT, FOREIGN KEY(x,y) REFERENCES p);"
        )
    tables = (await inspect_fixture(path, ("p", "c"))).schemas[0].tables
    assert tables[0].foreign_keys[0].referenced_column_ids == tables[1].primary_key


@pytest.mark.anyio
async def test_identifier_injection_is_quoted_and_keeps_exact_name(
    tmp_path: Path,
) -> None:
    path = tmp_path / "quote.sqlite"
    name = 'odd"; DROP TABLE safe; --'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE "odd""; DROP TABLE safe; --" ("(" INTEGER)')
        connection.execute("CREATE TABLE safe (id INTEGER)")
    before = path.read_bytes()
    table = (await inspect_fixture(path, (name,))).schemas[0].tables[0]
    assert table.name == name and table.columns[0].name == "("
    assert path.read_bytes() == before


@pytest.mark.anyio
async def test_primary_key_desc_exception_is_not_falsely_a_rowid_alias(
    tmp_path: Path,
) -> None:
    path = tmp_path / "desc.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY DESC)")
    with pytest.raises(DatabaseInspectionError) as caught:
        await inspect_fixture(path, ("t",))
    assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"


@pytest.mark.anyio
async def test_plain_unique_index_is_not_lost_or_marked_constraint(
    tmp_path: Path,
) -> None:
    path = tmp_path / "unique.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE t(a TEXT); CREATE UNIQUE INDEX ix ON t(a);"
        )
    table = (await inspect_fixture(path, ("t",))).schemas[0].tables[0]
    assert table.inspection is not None
    index = table.inspection.indexes[0]
    assert index.unique and index.origin == "index"
    assert not table.unique_constraints


@pytest.mark.anyio
async def test_reflection_order_follows_names_not_creation_order(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sorting.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE z(b INTEGER, a TEXT); CREATE TABLE a(id INTEGER);"
        )
    first = await inspect_fixture(path, ("z", "a"))
    second = await inspect_fixture(path, ("a", "z"))
    assert first == second
    columns = first.schemas[0].tables[1].columns
    assert [column.name for column in columns] == ["a", "b"]
    assert columns[0].inspection is not None
    assert columns[0].inspection.ordinal_position == 1


@pytest.mark.anyio
async def test_foreign_key_case_resolves_to_actual_catalog_identifiers(
    tmp_path: Path,
) -> None:
    path = tmp_path / "case.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            'CREATE TABLE "Parent" ("ID" INTEGER PRIMARY KEY);'
            'CREATE TABLE child ("ParentID" INTEGER REFERENCES parent(id));'
        )
    tables = (await inspect_fixture(path, ("Parent", "child"))).schemas[0].tables
    parent, child = tables
    assert child.foreign_keys[0].referenced_table_id == parent.table_id
    assert child.foreign_keys[0].referenced_column_ids == parent.primary_key


@pytest.mark.anyio
async def test_quoted_check_column_does_not_hide_table_check(tmp_path: Path) -> None:
    path = tmp_path / "check.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE t("CHECK" INTEGER, CHECK("CHECK" > 0))')
    table = (await inspect_fixture(path, ("t",))).schemas[0].tables[0]
    assert table.check_constraints == ('"CHECK" > 0',)


@pytest.mark.anyio
async def test_strict_any_has_no_numeric_affinity(tmp_path: Path) -> None:
    path = tmp_path / "strict.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            "CREATE TABLE ordinary(value ANY); CREATE TABLE strict_table(value ANY) STRICT;"
        )
    ordinary, strict = (
        (await inspect_fixture(path, ("ordinary", "strict_table"))).schemas[0].tables
    )
    assert ordinary.columns[0].inspection is not None
    assert ordinary.columns[0].inspection.data_type.affinity == "decimal"
    assert strict.inspection is not None and strict.inspection.strict
    assert strict.columns[0].inspection is not None
    assert strict.columns[0].inspection.data_type.affinity == "none"
