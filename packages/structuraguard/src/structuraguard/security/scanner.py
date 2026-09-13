"""Injection veto поверх обязательного existing SecurityScanner; run-local state."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    IssueSeverity,
    PipelineStatus,
    ProducerMetadata,
    ValidationIssue,
)
from structuraguard.contracts.injection import (
    InjectionAction,
    InjectionOrigin,
    InjectionPolicy,
    InjectionReport,
    InjectionSeverity,
    InjectionSignal,
    InjectionSummary,
)
from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.security import SecurityScanner

from ._privacy import failure, safe_failure
from .signals import InjectionDetector


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _bound_report(
    report: SecurityReport, request: SecurityScanRequest
) -> SecurityReport:
    if (
        type(report) is not SecurityReport
        or type(report.issues) is not tuple
        or len(report.issues) > 128
        or len(report.artifact_fingerprints) > 128
    ):
        raise failure("SECURITY_SCAN_FAILED") from None
    checked = SecurityReport.model_validate(report.model_dump(warnings="error"))
    if checked.injection is not None:
        raise failure("SECURITY_POLICY_INVALID") from None
    checked.require_request_binding(request)
    return checked


class InjectionAwareSecurityScanner:
    """Добавить injection restrictions к обязательному trusted base scanner.

    Args:
        scanner: Host SecurityScanner, выдающий exact-bound base reports.
        policy: Дополнительные action/caps, не замена source/privacy/egress policy.
        run_id: Тот же run, что в последующих SecurityScanRequest.
        routing_policy_id: Identity используемой router policy.
        routing_policy_fingerprint: Fingerprint той же неизменённой policy.
        clock: UTC часы для report metadata.
        monotonic: Монотонные секунды для scan deadlines.

    Instance принадлежит одному run/event loop. Создание не вызывает scanner/LLM.
    Накопленный source risk не сбрасывается после masking/fallback. Invalid config,
    binding, concurrent call и scan failure дают безопасный SecurityPolicyError.
    Ошибка/отмена закрывает instance. Events — bounded summaries без audit I/O;
    durable HMAC подключает host. Вложенный wrapper не поддержан."""

    def __init__(
        self,
        *,
        scanner: SecurityScanner,
        policy: InjectionPolicy,
        run_id: str,
        routing_policy_id: str,
        routing_policy_fingerprint: str,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if any(
            type(value) is not str or not 1 <= len(value) <= 128
            for value in (run_id, routing_policy_id, routing_policy_fingerprint)
        ) or not callable(getattr(scanner, "scan", None)):
            raise failure("SECURITY_POLICY_INVALID") from None
        self._detector = InjectionDetector(policy, monotonic=monotonic)
        self._scanner, self._clock = scanner, clock
        self._run_id, self._routing_id, self._routing_fingerprint = (
            run_id,
            routing_policy_id,
            routing_policy_fingerprint,
        )
        self._signals: tuple[InjectionSignal, ...] = ()
        self._events: list[InjectionSummary] = []
        self._scan_count = 0
        self._busy = self._failed = False

    @property
    def policy(self) -> InjectionPolicy:
        """Вернуть immutable policy дополнительных signals этого run."""
        return self._detector.policy

    @property
    def events(self) -> tuple[InjectionSummary, ...]:
        """Вернуть bounded safe summaries без текста/locations и audit I/O."""
        return tuple(self._events)

    def _begin(self) -> None:
        if self._failed or self._busy:
            raise failure("SECURITY_SCAN_FAILED") from None
        if self._scan_count >= self.policy.max_run_scans:
            self._failed = True
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        self._scan_count += 1
        self._busy = True

    def _remember(self, report: InjectionReport) -> InjectionReport:
        if len(self._signals) + len(report.signals) > self.policy.max_run_signals:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        added = tuple(
            signal.model_copy(
                update={
                    "location": signal.location.model_copy(
                        update={"scan_index": self._scan_count - 1}
                    )
                }
            )
            for signal in report.signals
        )
        self._signals += added
        severity = max(
            (s.severity for s in self._signals),
            key=list(InjectionSeverity).index,
            default=InjectionSeverity.NONE,
        )
        combined = InjectionReport.model_validate(
            {
                **report.model_dump(),
                "signals": self._signals,
                "severity": severity,
                "action": self.policy.action_for(severity),
            }
        )
        self._events.append(combined.safe_summary())
        return combined

    async def observe_source(
        self, text: str, *, origin: InjectionOrigin = InjectionOrigin.SOURCE_TEXT
    ) -> InjectionReport:
        """Сканировать text до redaction и сохранить риск в текущем run.

        origin описывает source/metadata/template. Возвращается InjectionReport;
        input остаётся untrusted. Расходует run scan/signal caps, не вызывает base
        scanner. SecurityPolicyError/отмена закрывают instance; partial clean нет."""
        self._begin()
        try:
            result = await self._detector.scan(text, origin=origin)
            return self._remember(result)
        except asyncio.CancelledError:
            self._failed = True
            raise asyncio.CancelledError from None
        except SecurityPolicyError as error:
            self._failed = True
            raise safe_failure(error) from None
        finally:
            self._busy = False

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        """Вернуть SecurityReport для точного outbound SecurityScanRequest.

        Вызывает base scanner после проверки bindings и сохраняет любой его отказ.
        Накопленный source risk только сужает decision; review не даёт approval,
        local_only не добавляет route. SecurityPolicyError/cancel закрывает instance.
        Сам метод LLM/DB не вызывает; I/O базового scanner принадлежит host."""
        self._begin()
        try:
            async with asyncio.timeout(self.policy.limits.max_time_ms / 1000):
                if (
                    type(request) is not SecurityScanRequest
                    or type(request.payload_json) is not str
                ):
                    raise failure() from None
                if len(request.payload_json) > self.policy.limits.max_chars:
                    raise failure("SECURITY_LIMIT_EXCEEDED") from None
                evidence = await self._detector.scan_json(request.payload_json)
                checked = SecurityScanRequest.model_validate(
                    request.model_dump(warnings="error")
                )
                if (
                    checked.run_id,
                    checked.routing_policy_id,
                    checked.routing_policy_fingerprint,
                    checked.purpose,
                ) != (
                    self._run_id,
                    self._routing_id,
                    self._routing_fingerprint,
                    "llm_input",
                ):
                    raise failure("SECURITY_POLICY_INVALID") from None
                injection = self._remember(evidence)
                base = _bound_report(await self._scanner.scan(checked), checked)
                if base.decision == "error":
                    self._failed = True
                return self._report(base, injection)
        except asyncio.CancelledError:
            self._failed = True
            raise asyncio.CancelledError from None
        except TimeoutError:
            self._failed = True
            raise failure("PROCESSING_TIMEOUT") from None
        except SecurityPolicyError as error:
            self._failed = True
            raise safe_failure(error) from None
        except BaseException as error:
            # Только внешний scanner: исключение может нести raw payload/credentials.
            self._failed = True
            if not isinstance(error, Exception):
                raise
            raise failure("SECURITY_SCAN_FAILED") from None
        finally:
            self._busy = False

    def _report(
        self,
        base: SecurityReport,
        injection: InjectionReport,
    ) -> SecurityReport:
        decision, status = base.decision, base.status
        blocked = base.blocked_items
        issues = base.issues
        if injection.signals:
            issues += (
                ValidationIssue(
                    code="SECURITY_INJECTION_SIGNAL",
                    message_key="SECURITY_INJECTION_SIGNAL",
                    severity=IssueSeverity.WARNING,
                ),
            )
            if decision == "allowed":
                status = PipelineStatus.COMPLETED_WITH_WARNINGS
        if decision == "allowed" and injection.action in {
            InjectionAction.NEEDS_REVIEW,
            InjectionAction.BLOCK,
        }:
            decision = (
                "review"
                if injection.action is InjectionAction.NEEDS_REVIEW
                else "blocked"
            )
            status = (
                PipelineStatus.NEEDS_REVIEW
                if decision == "review"
                else PipelineStatus.REJECTED_SECURITY
            )
            blocked = max(1, blocked)
        elif decision == "review" and injection.action is InjectionAction.BLOCK:
            decision, status = "blocked", PipelineStatus.REJECTED_SECURITY
        return SecurityReport.model_validate(
            {
                **base.model_dump(),
                "producer": ProducerMetadata(
                    component_id="injection_aware_scanner",
                    component_version="1.0.0",
                    sdk_version="0.3.0",
                ),
                "artifact_fingerprints": tuple(
                    dict.fromkeys(
                        (
                            *base.artifact_fingerprints,
                            canonical_sha256_value(base),
                            injection.policy_fingerprint,
                        )
                    )
                ),
                "decision": decision,
                "status": status,
                "scanned_items": max(1, base.scanned_items),
                "blocked_items": blocked,
                "issues": issues,
                "prompt_injection_detected": bool(injection.signals)
                or base.prompt_injection_detected,
                "injection": injection,
                "generated_at": self._clock(),
            }
        )
