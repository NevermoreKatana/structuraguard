"""Port детерминированной проверки недоверенного содержимого."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest


@runtime_checkable
class SecurityScanner(Protocol):
    """Проверяет bounded payload до следующей trust boundary."""

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        """Проверить bounded payload перед следующей trust boundary.

        Args:
            request: Canonical payload и security lineage текущего run.

        Returns:
            Typed security decision без raw payload и secret-bearing details.

        Security:
            Report хранит fingerprints и issues, но не копию сканируемых данных.
        """
