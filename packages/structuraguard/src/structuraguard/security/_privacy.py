"""Закрытые errors и общий budget одного полного privacy scan."""

import asyncio
import math
import time
from collections.abc import Callable

from structuraguard.contracts.privacy import ScanLimits
from structuraguard.exceptions import SecurityPolicyError


def failure(code: str = "SECURITY_INPUT_REJECTED") -> SecurityPolicyError:
    return SecurityPolicyError(
        error_code=code, message="Операция privacy policy отклонена."
    )


_SAFE_CODES = frozenset(
    {
        "SECURITY_INPUT_REJECTED",
        "SECURITY_LIMIT_EXCEEDED",
        "PROCESSING_TIMEOUT",
        "SECURITY_POLICY_INVALID",
        "SECURITY_PATTERN_UNSUPPORTED",
        "SECURITY_SCAN_FAILED",
        "SECURITY_REDACTION_DENIED",
        "SECURITY_MAP_ACCESS_DENIED",
        "SECURITY_MAP_INVALID",
        "SECURITY_MAP_EXPIRED",
        "SECURITY_MAP_KEY_INVALID",
        "SECURITY_CRYPTO_UNAVAILABLE",
    }
)


def safe_failure(error: SecurityPolicyError) -> SecurityPolicyError:
    """Даже typed ошибка внешнего adapter не даёт authority её raw diagnostics."""
    code = error.error_code
    return failure(
        code if type(code) is str and code in _SAFE_CODES else "SECURITY_SCAN_FAILED"
    )


class ScanBudget:
    def __init__(
        self, limits: ScanLimits, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limits, self.clock = limits, clock
        self.start = self.last = clock()
        if type(self.start) not in (int, float) or not math.isfinite(self.start):
            raise failure("SECURITY_POLICY_INVALID") from None
        self.chars = self.bytes = self.work = self.findings = 0
        self.check_time()

    def check_time(self) -> None:
        now = self.clock()
        if type(now) not in (float, int) or not math.isfinite(now) or now < self.last:
            raise failure("SECURITY_POLICY_INVALID") from None
        self.last = now
        if now - self.start >= self.limits.max_time_ms / 1000:
            raise failure("PROCESSING_TIMEOUT") from None

    def add_text(self, text: str) -> None:
        if type(text) is not str:
            raise failure() from None
        if len(text) > self.limits.max_chars - self.chars:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        try:
            size = len(text.encode("utf-8"))
        except UnicodeError:
            raise failure() from None
        if size > self.limits.max_bytes - self.bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        self.chars += len(text)
        self.bytes += size
        self.check_time()

    async def spend(self, work: int) -> None:
        if work > self.limits.max_work - self.work:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        self.work += work
        await asyncio.sleep(0)
        self.check_time()

    def finding(self) -> None:
        if self.findings >= self.limits.max_findings:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        self.findings += 1
        self.check_time()
