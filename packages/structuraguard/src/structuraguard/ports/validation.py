"""Read-only DB state отделён от pure rules и локальных catalog predicates."""

from typing import Protocol, runtime_checkable

from structuraguard.contracts.constraint_validation import (
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseCatalog


@runtime_checkable
class ConstraintReader(Protocol):
    """Проверить bounded keys в одном snapshot; не выдавать rows/SQL/writer handle."""

    async def read(
        self, request: ConstraintReadRequest, *, catalog: DatabaseCatalog
    ) -> ConstraintReadResult:
        """Проверить keys через read-only adapter и связать результат с request.

        Args:
            request: Bounded keys, catalog IDs и ожидаемые fingerprints.
            catalog: Catalog в доверенном scope target и column allowlist.

        Returns:
            ConstraintReadResult с boolean matches без исходных строк и SQL.

        Raises:
            ValidationError: Невалидный request/catalog либо intake budget.
            DatabaseInspectionError: Drift, access/storage/timeout failure.

        I/O ограничен одним read-only snapshot с cleanup. Ошибки и cancellation
        нельзя маскировать как отсутствие key; future-write TOCTOU не устраняется.
        """
        ...
