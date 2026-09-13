"""Сужение существующего adapter scope общей policy до reflection и SQL."""

from typing import cast

from structuraguard.contracts.database_policy import DatabasePolicy
from structuraguard.database.target import PostgreSQLTarget, SQLiteTarget
from structuraguard.domain.database_policy import authorize_database, denied


def bind_database_policy[T: PostgreSQLTarget | SQLiteTarget](
    target: T, policy: DatabasePolicy
) -> T:
    """Вернуть target того же типа с пересечением центрального и локального scope.

    target — trusted SQLiteTarget/PostgreSQLTarget; policy — точные allow/deny
    selectors и principals. Исходный target не меняется, I/O отсутствует.
    Пустое пересечение даёт SecurityPolicyError, неверный DTO — ValidationError.
    Полный column scope проверяет adapter до чтения definitions/comments;
    частичный constraint-aware каталог не публикуется. Grants проверяются в DB."""
    policy = DatabasePolicy.model_validate(policy.model_dump(warnings="error"))
    names = (
        target.include_tables
        if isinstance(target, PostgreSQLTarget)
        else tuple(("main", name) for name in target.include_tables)
    )
    selected = []
    for schema, table in names:
        from structuraguard.exceptions import SecurityPolicyError

        try:
            authorize_database(
                policy,
                schema=schema,
                table=table,
                dialect="postgresql"
                if isinstance(target, PostgreSQLTarget)
                else "sqlite",
            )
        except SecurityPolicyError:
            continue
        selected.append((schema, table))
    if not selected:
        raise denied()
    payload = target.model_dump()
    payload["security_policy"] = policy
    payload["include_tables"] = (
        tuple(selected)
        if isinstance(target, PostgreSQLTarget)
        else tuple(t for _, t in selected)
    )
    if isinstance(target, PostgreSQLTarget):
        payload["dsn"] = target.dsn
        payload["include_schemas"] = tuple(sorted({s for s, _ in selected}))
    return cast(T, type(target).model_validate(payload))
