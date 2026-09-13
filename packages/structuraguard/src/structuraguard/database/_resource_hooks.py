"""Instance-local query reservation до SQLAlchemy driver, без чтения payload."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.ports.resources import DatabaseResourceGuard

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine


def install_query_guard(engine: Engine, guard: DatabaseResourceGuard | None) -> None:
    """Привязать run counter только к owned engine; rollback/close не блокируются."""
    if guard is None:
        return
    from sqlalchemy import event

    def before_cursor_execute(
        connection: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        # SQL, binds и diagnostics намеренно не передаются security layer.
        guard.before_query()

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
