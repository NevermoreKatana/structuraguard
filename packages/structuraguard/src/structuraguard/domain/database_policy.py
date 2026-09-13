"""Детерминированный DB admission; SQL строят только существующие adapters."""

import re
from collections.abc import Iterable

from structuraguard.contracts.database_policy import DatabasePolicy
from structuraguard.exceptions import SecurityPolicyError


def denied(code: str = "TARGET_NOT_ALLOWED") -> SecurityPolicyError:
    return SecurityPolicyError(error_code=code, message="DB policy отклонила операцию.")


def safe_database_identifier(name: str) -> bool:
    return (
        type(name) is str
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name) is not None
    )


def _name(value: str, dialect: str) -> str:
    return value.lower() if dialect == "sqlite" else value


def authorize_database(
    policy: DatabasePolicy,
    *,
    schema: str,
    table: str,
    columns: Iterable[str] = (),
    operation: str = "inspect",
    dialect: str = "postgresql",
) -> None:
    """Deny всегда имеет приоритет; arbitrary SQL/DDL не является operation."""
    if operation not in {"inspect", "select", "insert", "update"} or dialect not in {
        "postgresql",
        "sqlite",
    }:
        raise denied("DATABASE_OPERATION_FORBIDDEN")
    if (
        not safe_database_identifier(schema)
        or not safe_database_identifier(table)
        or schema.lower().startswith(("pg_", "sg_staging_"))
        or schema.lower() in {"information_schema", "structuraguard_staging"}
        or table.lower().startswith("sqlite_")
    ):
        raise denied()

    def norm(value: str) -> str:
        return _name(value, dialect)

    pair = (norm(schema), norm(table))
    if (
        pair[0] not in {norm(s) for s in policy.allowed_schemas}
        or pair[0] in {norm(s) for s in policy.denied_schemas}
        or pair in {(norm(s), norm(t)) for s, t in policy.denied_tables}
    ):
        raise denied()
    rules = [
        r
        for r in policy.allowed_tables
        if (norm(r.schema_name), norm(r.table_name)) == pair
    ]
    if len(rules) != 1 or not rules[0].columns:
        raise denied()
    allowed = {norm(c) for c in rules[0].columns}
    forbidden = {
        norm(c) for s, t, c in policy.denied_columns if (norm(s), norm(t)) == pair
    }
    if isinstance(columns, str | bytes):
        raise denied("DATABASE_COLUMN_NOT_ALLOWED")
    count = 0
    for count, column in enumerate(columns, 1):
        if (
            count > 1024
            or not safe_database_identifier(column)
            or norm(column) not in allowed
            or norm(column) in forbidden
        ):
            raise denied("DATABASE_COLUMN_NOT_ALLOWED")
    if operation != "inspect" and count == 0:
        raise denied("DATABASE_COLUMN_NOT_ALLOWED")


def authorize_database_scope(
    policy: DatabasePolicy, names: Iterable[tuple[str, str]], *, dialect: str
) -> None:
    for index, (schema, table) in enumerate(names, 1):
        if index > 256:
            raise denied("SECURITY_LIMIT_EXCEEDED")
        authorize_database(policy, schema=schema, table=table, dialect=dialect)
