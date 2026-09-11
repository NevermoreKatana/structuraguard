"""Port детерминированной проверки недоверенного содержимого."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from structuraguard.contracts.profiling import (
    PIIClassificationRequest,
    PIIClassificationResult,
)
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


@runtime_checkable
class PIIClassifier(Protocol):
    """Классифицирует bounded evidence без raw dataset и egress authority."""

    async def classify(
        self, request: PIIClassificationRequest
    ) -> PIIClassificationResult:
        """Классифицировать ограниченные признаки одного semantic field.

        Args:
            request: Field/input binding, labels, counts/categories и минимальный
                класс. Raw examples и source/DB/network handles не передаются.

        Returns:
            PIIClassificationResult с тем же binding, состоянием и покрытием;
            класс не ниже minimum_classification и локального baseline profiler.

        Raises:
            ValueError: Нарушен контракт request или его fingerprint.

        Profiler повторно проверяет ответ и преобразует сбой адаптера в
        NORMALIZED_PROFILE_CLASSIFICATION_FAILED. Отмена сохраняется.
        Код адаптера доверенный; этот port не изолирует его и не выдаёт
        SecurityApproval. Labels остаются чувствительными данными.
        """
        ...
