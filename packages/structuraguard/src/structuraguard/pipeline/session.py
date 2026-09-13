"""Один run: state, safe errors, events и общие resource budgets."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from uuid import UUID

from pydantic import TypeAdapter
from pydantic import ValidationError as ContractError

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.audit import AuditKind, SecurityAuditEvent
from structuraguard.contracts.common import (
    IssueSeverity,
    ProducerMetadata,
    TransactionOutcome,
    ValidationIssue,
)
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.orchestration import (
    IngestResult,
    PipelineFailure,
    SDKSecurityReport,
)
from structuraguard.contracts.reports import (
    AuditEvent,
    SecurityReport,
    SecurityScanRequest,
)
from structuraguard.contracts.security import Resource
from structuraguard.contracts.staging import StagingRunStatus
from structuraguard.exceptions import SecurityPolicyError, StructuraGuardError
from structuraguard.parsing import SemanticParsingSession
from structuraguard.security.audit import AuditChain
from structuraguard.security.scanner import InjectionAwareSecurityScanner
from structuraguard.security.session import SecuritySession
from structuraguard.structure.hybrid import HybridAnalysis

from .composition import DatabaseBinding, SDKDependencies
from .state import TERMINAL, check_transition


def producer() -> ProducerMetadata:
    return ProducerMetadata(
        component_id="sdk_orchestrator", component_version="1.0.0", sdk_version="0.3.0"
    )


def opaque_id(dependencies: SDKDependencies) -> UUID:
    """Выделить nonce, совместимый с PII guard AuditEvent, максимум 16 попыток.

    Случайная UUID иногда содержит Luhn-совпадение через дефисы. Проверяем ID
    до привязки run и не ослабляем существующий wire guard ради такого совпадения.
    """
    validator: TypeAdapter[str] = TypeAdapter(
        AuditEvent.model_fields["run_id"].rebuild_annotation()
    )
    for _ in range(16):
        value = dependencies.new_id()
        if type(value) is not UUID:
            break
        try:
            validator.validate_python(f"run-{value}")
        except ContractError:
            continue
        return value
    raise StructuraGuardError(
        error_code="SDK_IDENTIFIER_REJECTED", message="SDK_IDENTIFIER_REJECTED"
    )


class PipelineError(StructuraGuardError):
    """Ошибка пошаговой обработки с частичным IngestResult в атрибуте result.

    Из ``result.errors`` выбирается последний error_code, иначе SDK_NEEDS_REVIEW.
    Безопасные details не содержат исходный exception/payload; ``result`` может
    содержать PII и требует отдельной защиты. Создание ошибки не выполняет I/O.
    Конечные ingest/execute/analyze возвращают этот результат, а отмена до commit
    распространяется отдельно как CancelledError.
    """

    def __init__(self, result: IngestResult) -> None:
        code = result.errors[-1].code if result.errors else "SDK_NEEDS_REVIEW"
        super().__init__(error_code=code, message=code, run_id=result.run_id)
        self.result = result


class RunSession:
    """Runtime принадлежит source lease; один активный stage на session."""

    def __init__(
        self, dependencies: SDKDependencies, owner: object, *, dry_run: bool
    ) -> None:
        self.dependencies = dependencies
        self.owner = owner
        self.id = opaque_id(dependencies)
        policy = dependencies.security
        policy = policy.narrow(
            policy.limits.model_copy(
                update={
                    "max_file_bytes": min(
                        policy.limits.max_file_bytes, dependencies.max_snapshot_bytes
                    ),
                    "max_stream_bytes": min(
                        policy.limits.max_stream_bytes, dependencies.max_snapshot_bytes
                    ),
                    "max_records": min(
                        policy.limits.max_records, dependencies.max_records
                    ),
                    "max_chunks": min(
                        policy.limits.max_chunks, dependencies.max_batches
                    ),
                }
            )
        )
        if dependencies.routing:
            budget = dependencies.routing.budget
            limits = policy.limits.model_copy(
                update={
                    "max_llm_calls": min(policy.limits.max_llm_calls, budget.max_calls),
                    "max_llm_tokens": min(
                        policy.limits.max_llm_tokens, budget.max_tokens
                    ),
                    "max_llm_time_ms": min(
                        policy.limits.max_llm_time_ms, budget.max_time_ms
                    ),
                }
            )
            policy = policy.model_copy(update={"limits": limits})
        self.resources = SecuritySession(
            policy, run_id=self.id, clock=dependencies.clock
        )
        self.result = IngestResult(
            run_id=self.run_id,
            status=S.CREATED,
            dry_run=dry_run,
            security_report=SDKSecurityReport(
                policy_fingerprint=self.resources.policy.fingerprint
            ),
        )
        self.semantic: SemanticParsingSession | None = None
        self.analysis: HybridAnalysis | None = None
        self.database: DatabaseBinding | None = None
        self.staging_context: StagingContext | None = None
        self.scanner: InjectionAwareSecurityScanner | None = None
        self.scans: list[SecurityReport] = []
        self.audit: AuditChain | None = None
        self.closed = self.busy = False
        self.executing = False
        self.retained_bytes = 0
        self.started = False

    @property
    def run_id(self) -> str:
        return f"run-{self.id}"

    async def bounded[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        resource: Resource = Resource.PROCESSING_TIME_MS,
    ) -> T:
        """Stage сохраняет typed primary error; resource guard задаёт только deadline."""
        async with asyncio.timeout(self.resources.remaining_seconds(resource)):
            result = await operation()
        self.resources.check_deadline()
        return result

    def snapshot(self) -> IngestResult:
        security = self.result.security_report.model_copy(
            update={
                "resources": self.resources.events,
                "scans": tuple(self.scans),
                "injection": self.scanner.events
                if self.scanner
                else self.result.security_report.injection,
            }
        )
        self.result = self.result.model_copy(update={"security_report": security})
        return IngestResult.model_validate(self.result.model_dump(mode="python"))

    def set(self, **fields: object) -> None:
        self.result = self.result.model_copy(update=fields)

    def ensure_open(self) -> None:
        if self.closed:
            raise StructuraGuardError(
                error_code="SOURCE_SNAPSHOT_EXPIRED", message="SOURCE_SNAPSHOT_EXPIRED"
            )
        self.resources.check_deadline()

    def retain(self, size: int) -> None:
        self.retained_bytes += size
        if self.retained_bytes > self.dependencies.max_snapshot_bytes:
            raise SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED", message="SECURITY_LIMIT_EXCEEDED"
            )

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        if self.scanner is None:
            raise SecurityPolicyError(
                error_code="LLM_DATA_ROUTING_FORBIDDEN",
                message="LLM_DATA_ROUTING_FORBIDDEN",
            )
        report = await self.scanner.scan(request)
        report.require_request_binding(request)
        self.scans.append(report)
        return report

    async def event(self, event_type: str, status: S) -> None:
        fingerprints = tuple(
            fp
            for fp in (
                self.resources.policy.fingerprint,
                self.result.source_fingerprint,
                self.result.parse_plan_fingerprint,
                self.result.normalized_fingerprint,
                self.result.database_fingerprint,
                self.result.mapping_plan_fingerprint,
            )
            if fp
        )
        gate = None
        if status in {S.COMPLETED, S.COMPLETED_WITH_WARNINGS, S.REJECTED_SECURITY}:
            # Evidence относится к выполненным local policy gates. Оно никогда
            # не передаётся LLM как approval: egress имеет отдельный bound scan.
            policy_fp = self.resources.policy.fingerprint
            payload_fp = canonical_sha256_value(self.snapshot().security_report)
            content_fp = (
                self.result.source_fingerprint
                or self.result.database_fingerprint
                or policy_fp
            )
            blocked = status is S.REJECTED_SECURITY
            gate = SecurityReport(
                request_id=f"gate-{self.id}",
                run_id=self.run_id,
                purpose="source_content",
                content_fingerprint=content_fp,
                payload_fingerprint=payload_fp,
                data_classification=self.result.security_report.privacy[
                    -1
                ].classification
                if self.result.security_report.privacy
                else self.dependencies.privacy.baseline,
                routing_policy_id="sdk_local_gates",
                routing_policy_fingerprint=policy_fp,
                redaction_fingerprint=payload_fp,
                producer=producer(),
                decision="blocked" if blocked else "allowed",
                status=S.REJECTED_SECURITY if blocked else S.COMPLETED,
                artifact_fingerprints=tuple(dict.fromkeys((content_fp, payload_fp))),
                scanned_items=1,
                blocked_items=int(blocked),
                issues=(
                    ValidationIssue(
                        code="SECURITY_INPUT_REJECTED",
                        message_key="SECURITY_INPUT_REJECTED",
                        severity=IssueSeverity.ERROR,
                    ),
                )
                if blocked
                else (),
                generated_at=self.dependencies.clock(),
            )
            fingerprints = (*fingerprints, canonical_sha256_value(gate))
        event = AuditEvent(
            event_id=f"event-{opaque_id(self.dependencies)}",
            run_id=self.run_id,
            occurred_at=self.dependencies.clock(),
            status=status,
            event_type=event_type,
            artifact_fingerprints=tuple(dict.fromkeys(fingerprints)),
            producer=producer(),
            security_report=gate,
            security_report_fingerprint=canonical_sha256_value(gate) if gate else None,
            redaction_fingerprint=gate.redaction_fingerprint if gate else None,
        )
        if len(self.result.audit_events) >= 256:
            raise StructuraGuardError(
                error_code="SECURITY_LIMIT_EXCEEDED", message="SECURITY_LIMIT_EXCEEDED"
            )
        self.set(audit_events=(*self.result.audit_events, event))
        for hook in self.dependencies.hooks:
            await hook(event)

    async def signed(self, kind: AuditKind, *, status: str = "completed") -> None:
        if self.audit is None:
            return
        actor = self.dependencies.actor_id
        assert actor is not None
        event = SecurityAuditEvent.model_validate(
            {
                "event_id": self.dependencies.new_id(),
                "run_id": self.id,
                "actor_id": actor,
                "occurred_at": self.dependencies.clock(),
                "kind": kind,
                "status": status,
                "decision": None
                if status == "started"
                else "allowed"
                if status == "completed"
                else "review"
                if status == "review"
                else "blocked"
                if status == "rejected"
                else "error",
                "policy_fingerprint": self.resources.policy.fingerprint,
                "source_fingerprint": self.result.source_fingerprint,
                "database_fingerprint": self.result.database_fingerprint,
                "mapping_fingerprint": self.result.mapping_plan_fingerprint,
            }
        )
        envelope = await self.audit.append(event)
        self.set(audit_references=(*self.result.audit_references, envelope.head))

    async def perform[T](
        self,
        stage: S,
        operation: Callable[[], Awaitable[T]],
        *,
        transactional: bool = False,
    ) -> T:
        if self.busy:
            raise StructuraGuardError(error_code="SDK_RUN_BUSY", message="SDK_RUN_BUSY")
        self.busy = True
        failure: IngestResult | None = None
        cancelled = False
        try:
            self.ensure_open()
            if not self.started:
                if self.dependencies.audit:
                    self.audit = self.dependencies.audit(self.resources)
                if self.audit:
                    await self.bounded(self.audit.check_key)
                await self.bounded(
                    lambda: self.signed(AuditKind.RUN_STARTED, status="started")
                )
                await self.bounded(lambda: self.event("run_started", S.CREATED))
                self.started = True
            if self.result.status is not stage:
                check_transition(self.result.status, stage)
                self.set(status=stage)
            elif stage not in {S.PARSE_PLAN_VALIDATING, S.MAPPING_PLAN_VALIDATING}:
                raise StructuraGuardError(
                    error_code="SDK_INVALID_TRANSITION",
                    message="SDK_INVALID_TRANSITION",
                )
            await self.bounded(lambda: self.event("stage_started", stage))
            result = (
                await operation() if transactional else await self.bounded(operation)
            )
            if self.result.transaction_outcome is TransactionOutcome.COMMITTED:
                try:
                    async with asyncio.timeout(self.dependencies.cleanup_seconds):
                        await self.event("stage_completed", stage)
                except BaseException as error:
                    if not isinstance(error, (Exception, asyncio.CancelledError)):
                        raise
                    self.set(
                        warnings=(*self.result.warnings, "SDK_POST_COMMIT_HOOK_FAILED")
                    )
            else:
                await self.bounded(lambda: self.event("stage_completed", stage))
                kind = {
                    S.TECHNICAL_PARSING: AuditKind.PARSER_FINISHED,
                    S.VALIDATING: AuditKind.VALIDATION_FINISHED,
                }.get(stage)
                if kind:
                    await self.bounded(lambda: self.signed(kind))
            return result
        except PipelineError as error:
            failure = error.result
        except asyncio.CancelledError:
            self.resources.record_cancelled()
            if self.result.transaction_outcome is TransactionOutcome.COMMITTED:
                self.set(
                    warnings=(*self.result.warnings, "LOAD_CANCELLED_AFTER_COMMIT")
                )
                await self.terminal(S.COMPLETED_WITH_WARNINGS)
                failure = self.snapshot()
            else:
                await self.terminal(S.CANCELLED, "SDK_CANCELLED")
                cancelled = True
        except BaseException as error:
            # Единственная внешняя boundary: произвольный adapter/hook exception
            # не имеет полномочий публиковать raw message/notes/exception chain.
            if not isinstance(error, Exception):
                raise
            code = (
                error.error_code
                if isinstance(error, StructuraGuardError)
                else "PROCESSING_TIMEOUT"
                if isinstance(error, TimeoutError)
                else "SDK_STAGE_FAILED"
            )
            if code == "SECURITY_RUN_CLOSED" and self.resources.events:
                code = self.resources.events[0].code
            outcome = (
                S.REJECTED_SECURITY
                if isinstance(error, SecurityPolicyError)
                and code not in {"PROCESSING_TIMEOUT", "SECURITY_OPERATION_FAILED"}
                else S.FAILED
            )
            if code in {
                "DATABASE_SCHEMA_DRIFT",
                "DATABASE_FINGERPRINT_MISMATCH",
                "SOURCE_FINGERPRINT_MISMATCH",
                "DRY_RUN_PROVENANCE_UNVERIFIED",
            }:
                outcome = S.NEEDS_REVIEW
            if (
                code == "LOAD_OUTCOME_UNKNOWN"
                and self.result.transaction_outcome is not TransactionOutcome.COMMITTED
            ):
                self.set(transaction_outcome=TransactionOutcome.UNKNOWN)
            elif (
                self.result.transaction_outcome is TransactionOutcome.ROLLED_BACK
                and code != "PROCESSING_TIMEOUT"
            ):
                outcome = S.ROLLED_BACK
            if (
                isinstance(error, StructuraGuardError)
                and error.details.get("audit_gap") is True
            ):
                self.set(warnings=(*self.result.warnings, "AUDIT_GAP"))
            causes = (
                (type(error).__name__,)
                if type(error).__module__
                in {"builtins", "pydantic_core._pydantic_core"}
                else ()
            )
            if self.result.transaction_outcome is TransactionOutcome.COMMITTED:
                self.set(warnings=(*self.result.warnings, code))
                await self.terminal(S.COMPLETED_WITH_WARNINGS)
            else:
                await self.terminal(outcome, code, causes=causes)
            failure = self.snapshot()
        finally:
            self.busy = False
        # Выход из except освобождает raw context adapter/hook exception.
        if cancelled:
            raise asyncio.CancelledError from None
        assert failure is not None
        raise PipelineError(failure) from None

    async def terminal(
        self, status: S, code: str | None = None, *, causes: tuple[str, ...] = ()
    ) -> None:
        if self.result.status in TERMINAL:
            return
        previous = self.result.status
        if code:
            self.set(
                errors=(
                    *self.result.errors,
                    PipelineFailure(code=code, stage=previous, cause_codes=causes),
                )
            )
        cancelled = False
        try:
            async with asyncio.timeout(self.dependencies.cleanup_seconds):
                await self.signed(
                    AuditKind.RUN_FAILED
                    if status in {S.FAILED, S.CANCELLED, S.ROLLED_BACK}
                    else AuditKind.RUN_FINISHED,
                    status="completed"
                    if status in {S.COMPLETED, S.COMPLETED_WITH_WARNINGS}
                    else "cancelled"
                    if status is S.CANCELLED
                    else "review"
                    if status is S.NEEDS_REVIEW
                    else "rejected"
                    if status is S.REJECTED_SECURITY
                    else "failed",
                )
                await self.event("run_finished", status)
        except BaseException as error:
            if not isinstance(error, (Exception, asyncio.CancelledError)):
                raise
            cancelled = isinstance(error, asyncio.CancelledError)
            self.set(warnings=(*self.result.warnings, "AUDIT_GAP"))
            if self.result.transaction_outcome is TransactionOutcome.COMMITTED:
                status = S.COMPLETED_WITH_WARNINGS
            elif cancelled:
                status = S.CANCELLED
            elif status in {S.COMPLETED, S.COMPLETED_WITH_WARNINGS}:
                status = S.FAILED
                self.set(
                    errors=(
                        *self.result.errors,
                        PipelineFailure(code="AUDIT_GAP", stage=previous),
                    )
                )
        check_transition(previous, status)
        self.set(status=status)
        try:
            async with asyncio.timeout(self.dependencies.cleanup_seconds):
                await self.cleanup_staging()
        except BaseException as error:
            if not isinstance(error, (Exception, asyncio.CancelledError)):
                raise
            self.set(warnings=(*self.result.warnings, "SDK_CLEANUP_FAILED"))
            cancelled = cancelled or isinstance(error, asyncio.CancelledError)
        if (
            cancelled
            and self.result.transaction_outcome is not TransactionOutcome.COMMITTED
        ):
            self.set(status=S.CANCELLED)
            raise asyncio.CancelledError from None

    async def stop(self, code: str) -> None:
        await self.terminal(S.NEEDS_REVIEW, code)
        raise PipelineError(self.snapshot())

    async def cleanup_staging(self) -> None:
        """Не объявлять EXECUTING/UNKNOWN откатанными без transaction evidence."""
        if not (
            self.staging_context
            and self.database
            and self.database.staging
            and self.result.transaction_outcome is TransactionOutcome.NOT_STARTED
        ):
            return
        staged = await self.database.staging.get_run(self.staging_context)
        if staged.status in {StagingRunStatus.OPEN, StagingRunStatus.SEALED}:
            await self.database.staging.transition(
                self.staging_context,
                expected_revision=staged.revision,
                status=StagingRunStatus.CANCELLED
                if self.result.status is S.CANCELLED
                else StagingRunStatus.FAILED,
            )

    async def aclose(self) -> None:
        if self.closed:
            return

        async def cleanup() -> None:
            async with asyncio.timeout(self.dependencies.cleanup_seconds):
                try:
                    await self.cleanup_staging()
                finally:
                    if self.semantic:
                        await self.semantic.aclose()

        task = asyncio.create_task(cleanup())
        cancelled = False
        try:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
            task.result()
        except BaseException as error:
            if not isinstance(error, (Exception, asyncio.CancelledError)):
                raise
            self.set(warnings=(*self.result.warnings, "SDK_CLEANUP_FAILED"))
            if self.result.status is S.COMPLETED:
                self.set(status=S.COMPLETED_WITH_WARNINGS)
        finally:
            self.closed = True
        if cancelled:
            if self.result.transaction_outcome is TransactionOutcome.COMMITTED:
                self.set(
                    status=S.COMPLETED_WITH_WARNINGS,
                    warnings=(*self.result.warnings, "LOAD_CANCELLED_AFTER_COMMIT"),
                )
            else:
                raise asyncio.CancelledError from None

    @contextmanager
    def execution(self, *, dry_run: bool) -> Iterator[None]:
        """Защитить выбор режима и весь execute, включая terminal hooks.

        Между проверкой и резервированием нет await. Stage busy недостаточен:
        он уже снят при terminal hooks, которые также могут вызвать SDK повторно.
        """
        if self.busy or self.executing:
            raise StructuraGuardError(error_code="SDK_RUN_BUSY", message="SDK_RUN_BUSY")
        self.executing = True
        try:
            self.set(dry_run=dry_run)
            yield
        finally:
            self.executing = False
