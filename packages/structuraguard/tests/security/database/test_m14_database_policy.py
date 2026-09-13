import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.database_policy import DatabasePolicy, DatabaseTableRule
from structuraguard.database import SQLiteDatabaseAdapter, SQLiteTarget
from structuraguard.domain.database_policy import authorize_database
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security.database import bind_database_policy


def policy(**overrides: object) -> DatabasePolicy:
    return DatabasePolicy.model_validate(
        {
            "allowed_schemas": ("main",),
            "allowed_tables": (
                DatabaseTableRule(
                    schema_name="main", table_name="items", columns=("id", "amount")
                ),
            ),
            "inspector_principal": "inspector",
            "writer_principal": "writer",
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "operation",
    [
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "DELETE",
        "select * from items",
        "unknown",
    ],
)
def test_ddl_and_free_sql_are_never_operations(operation: str) -> None:
    with pytest.raises(SecurityPolicyError, match="DATABASE_OPERATION_FORBIDDEN"):
        authorize_database(
            policy(), schema="main", table="items", columns=("id",), operation=operation
        )


@pytest.mark.parametrize(
    "change",
    [
        {"denied_schemas": ("main",)},
        {"denied_tables": (("main", "items"),)},
        {"denied_columns": (("main", "items", "id"),)},
        {"allowed_schemas": ()},
        {"allowed_tables": ()},
    ],
)
def test_denies_override_allow(change: dict[str, object]) -> None:
    with pytest.raises(SecurityPolicyError):
        authorize_database(
            policy(**change), schema="main", table="items", columns=("id",)
        )


@pytest.mark.parametrize(
    "schema,table",
    [
        ("pg_catalog", "pg_class"),
        ("information_schema", "tables"),
        ("main", "sqlite_master"),
        ("main", "items;DROP"),
        ("main", 'items"'),
        ("sg_staging_audit_x", "chain_events"),
    ],
)
def test_system_and_unsafe_identifiers_denied(schema: str, table: str) -> None:
    with pytest.raises(SecurityPolicyError):
        authorize_database(policy(), schema=schema, table=table)


def test_same_principal_invalid_and_unsupported_column_denied() -> None:
    with pytest.raises(ValidationError):
        policy(writer_principal="inspector")
    authorize_database(policy(), schema="main", table="items", columns=("id", "amount"))
    with pytest.raises(SecurityPolicyError, match="DATABASE_COLUMN_NOT_ALLOWED"):
        authorize_database(
            policy(), schema="main", table="items", columns=("id", "amount", "extra")
        )


@pytest.mark.anyio
@pytest.mark.parametrize("extra", [False, True])
async def test_sqlite_column_admission_before_definitions(
    tmp_path: Path, extra: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "scope.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE items(id INTEGER, amount INTEGER"
            + (", hidden TEXT DEFAULT 'restricted-canary'" if extra else "")
            + ")"
        )
    from structuraguard.database import sqlite as adapter

    original = adapter._SQLiteReader.definition
    definitions: list[str] = []

    def definition(reader: adapter._SQLiteReader, name: str, kind: str) -> str | None:
        definitions.append(name)
        return original(reader, name, kind)

    monkeypatch.setattr(adapter._SQLiteReader, "definition", definition)
    target = bind_database_policy(
        SQLiteTarget(path=path, target_id="main", include_tables=("items",)), policy()
    )
    request = DatabaseInspectionRequest(
        target_id="main", target_policy_fingerprint=target.policy_fingerprint
    )
    if extra:
        with pytest.raises(SecurityPolicyError, match="DATABASE_COLUMN_NOT_ALLOWED"):
            await SQLiteDatabaseAdapter(target).inspect(request)
        assert not definitions
    else:
        result = await SQLiteDatabaseAdapter(target).inspect(request)
        assert {c.name for c in result.schemas[0].tables[0].columns} == {"id", "amount"}


def test_sqlite_deny_case_folding_and_no_scope_expansion(tmp_path: Path) -> None:
    with pytest.raises(SecurityPolicyError):
        authorize_database(
            policy(denied_columns=(("MAIN", "ITEMS", "ID"),)),
            schema="main",
            table="items",
            columns=("id",),
            dialect="sqlite",
        )
    target = SQLiteTarget(
        path=tmp_path / "scope.sqlite", target_id="main", include_tables=("other",)
    )
    with pytest.raises(SecurityPolicyError):
        bind_database_policy(target, policy())
