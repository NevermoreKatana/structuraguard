"""Центральная DB policy ограничивает SQLite EXISTS даже при rebound catalog."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from tests.fakes.mapping import refs

from structuraguard.contracts.constraint_validation import (
    ConstraintReadPolicy,
    ConstraintReadRequest,
)
from structuraguard.contracts.database import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.contracts.database_policy import DatabasePolicy, DatabaseTableRule
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.database.constraint_reader import DatabaseConstraintReader
from structuraguard.database.target import InspectionLimits
from structuraguard.exceptions import DatabaseInspectionError, SecurityPolicyError


def policy(**overrides: object) -> DatabasePolicy:
    return DatabasePolicy.model_validate(
        {
            "allowed_schemas": ("main",),
            "allowed_tables": (
                DatabaseTableRule(
                    schema_name="main",
                    table_name="items",
                    columns=("id", "email", "note"),
                ),
            ),
            "inspector_principal": "inspector",
            "writer_principal": "writer",
            **overrides,
        }
    )


async def bound_request(
    tmp_path: Path,
    configured: DatabasePolicy,
    *,
    limits: InspectionLimits | None = None,
) -> tuple[SQLiteTarget, DatabaseCatalog, ConstraintReadRequest]:
    path = tmp_path / "constraints.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE items(id INTEGER PRIMARY KEY, email TEXT UNIQUE, "
            "note TEXT DEFAULT 'restricted-canary')"
        )
        connection.execute(
            "INSERT INTO items(id, email) VALUES (?, ?)", (1, "alice@example.test")
        )
    legacy = SQLiteTarget(path=path, target_id="main", include_tables=("items",))
    catalog = await SQLiteDatabaseAdapter(legacy).inspect(
        DatabaseInspectionRequest(
            target_id=legacy.target_id,
            target_policy_fingerprint=legacy.policy_fingerprint,
        )
    )
    target = SQLiteTarget(
        path=path,
        target_id=legacy.target_id,
        include_tables=legacy.include_tables,
        security_policy=configured,
        limits=limits or legacy.limits,
    )
    # Fingerprint связывает данные, но не выдаёт authority после сужения policy.
    catalog = catalog.model_copy(
        update={"target_policy_fingerprint": target.policy_fingerprint}
    )
    table = catalog.schemas[0].tables[0]
    columns = {column.name: column.column_id for column in table.columns}
    request = ConstraintReadRequest.model_validate(
        {
            "target_id": target.target_id,
            "target_policy_fingerprint": target.policy_fingerprint,
            "database_fingerprint": catalog.database_fingerprint,
            "lookups": (
                {
                    "lookup_id": "email",
                    "table_id": table.table_id,
                    "column_ids": (columns["email"],),
                    "values": ({"kind": "string", "value": "alice@example.test"},),
                    "identity_column_ids": (columns["id"],),
                    "identity_values": ({"kind": "integer", "value": 1},),
                },
            ),
        }
    )
    return target, catalog, request


@contextmanager
def recorded_statements() -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(
        connection: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        statements.append(statement.upper())

    event.listen(Engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", capture)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"denied_schemas": ("main",)},
        {"denied_schemas": ("MAIN",)},
        {"denied_tables": (("main", "items"),)},
        {"denied_tables": (("MAIN", "ITEMS"),)},
        {"allowed_schemas": ()},
        {"allowed_tables": ()},
        {
            "allowed_tables": (
                DatabaseTableRule(schema_name="main", table_name="items", columns=()),
            )
        },
    ],
    ids=[
        "schema",
        "schema-case",
        "table",
        "table-case",
        "no-schema",
        "no-table",
        "no-columns",
    ],
)
async def test_central_scope_denies_rebound_catalog_before_connection(
    tmp_path: Path, change: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, catalog, request = await bound_request(tmp_path, policy(**change))

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        pytest.fail("Запрещённый central scope дошёл до SQLite connection")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    with pytest.raises(SecurityPolicyError) as error:
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(catalog))
        ).read(request, catalog=catalog)
    assert error.value.error_code == "TARGET_NOT_ALLOWED"


@pytest.mark.anyio
@pytest.mark.parametrize("column", ["email", "id", "note", "EMAIL"])
async def test_central_column_deny_precedes_definitions_and_exists(
    tmp_path: Path, column: str
) -> None:
    target, catalog, request = await bound_request(
        tmp_path, policy(denied_columns=(("MAIN", "ITEMS", column),))
    )
    with (
        recorded_statements() as statements,
        pytest.raises(SecurityPolicyError) as error,
    ):
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(catalog))
        ).read(request, catalog=catalog)
    assert error.value.error_code == "DATABASE_COLUMN_NOT_ALLOWED"
    assert statements
    assert not any("SQLITE_SCHEMA" in sql or "EXISTS" in sql for sql in statements)
    assert "restricted-canary" not in str(error.value)
    assert "alice@example.test" not in str(error.value)
    assert str(target.path) not in str(error.value)


@pytest.mark.anyio
async def test_new_unlisted_column_denied_before_definitions_and_exists(
    tmp_path: Path,
) -> None:
    target, catalog, request = await bound_request(tmp_path, policy())
    with sqlite3.connect(target.path) as connection:
        connection.execute("ALTER TABLE items ADD COLUMN hidden TEXT DEFAULT 'canary'")
    with (
        recorded_statements() as statements,
        pytest.raises(SecurityPolicyError) as error,
    ):
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=refs(catalog))
        ).read(request, catalog=catalog)
    assert error.value.error_code == "DATABASE_COLUMN_NOT_ALLOWED"
    assert not any("SQLITE_SCHEMA" in sql or "EXISTS" in sql for sql in statements)


@pytest.mark.anyio
@pytest.mark.parametrize("max_columns", [3, 2])
async def test_column_preflight_limit_boundary(
    tmp_path: Path, max_columns: int
) -> None:
    target, catalog, request = await bound_request(
        tmp_path, policy(), limits=InspectionLimits(max_columns=max_columns)
    )
    reader = DatabaseConstraintReader(
        target, policy=ConstraintReadPolicy(allow_columns=refs(catalog))
    )
    with recorded_statements() as statements:
        if max_columns == 3:
            result = await reader.read(request, catalog=catalog)
            assert result.matches[0].exists and not result.matches[0].conflicts
            assert any("EXISTS" in sql for sql in statements)
        else:
            with pytest.raises(DatabaseInspectionError) as error:
                await reader.read(request, catalog=catalog)
            assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"
            assert not any(
                "SQLITE_SCHEMA" in sql or "EXISTS" in sql for sql in statements
            )


@pytest.mark.anyio
async def test_central_allow_does_not_override_local_constraint_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, catalog, request = await bound_request(tmp_path, policy())

    def forbidden_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        pytest.fail("Локальный column deny дошёл до SQLite connection")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    with pytest.raises(DatabaseInspectionError) as error:
        await DatabaseConstraintReader(
            target, policy=ConstraintReadPolicy(allow_columns=())
        ).read(request, catalog=catalog)
    assert error.value.error_code == "TARGET_NOT_ALLOWED"
