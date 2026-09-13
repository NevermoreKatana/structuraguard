"""PostgreSQL load из sealed staging с atomic/quarantine и явным durable ledger."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunRequest,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.staging import StagingRun, StagingRunStatus
from structuraguard.exceptions import StructuraGuardError
from structuraguard.loading.projection import checked, failure, prepare
from structuraguard.loading.staged_input import finish_staging, verify_staging
from structuraguard.ports.stores import RunStagingStore

from ._inspection import inspection_slot
from ._load_transaction import execute_transaction
from .dry_run import PostgreSQLDryRunPlanner
from .postgresql import PostgreSQLDatabaseAdapter
from .writer_target import PostgreSQLWriterTarget, writer_scope


class PostgreSQLLoader:
    """Писать отдельным least-privilege user; один COMMIT на весь sealed run.

    Args:
        target: Trusted writer credentials и inspector scope того же endpoint.
        policy: Atomic/quarantine admission, write tables, bulk и optional ledger.
        staging: Trusted RunStagingStore; loader не вызывает begin/stage/bootstrap.
        clock: UTC часы для expiry и отчёта; None использует datetime.now(UTC).

    Raises:
        LoadError: Неверные target/policy либо несовпадающий writer principal.
        DatabaseInspectionError: Некорректная конфигурация inspector.

    Конструктор не выполняет I/O. Statement/row values не принимаются извне.
    Для metadata-only staging загрузка запрещена. External artifact authenticity
    остаётся у владельца; hashes связывают supplied snapshot с sealed metadata.
    """

    def __init__(
        self,
        target: PostgreSQLWriterTarget,
        *,
        policy: PostgreSQLLoadPolicy,
        staging: RunStagingStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(target) is not PostgreSQLWriterTarget:
            raise failure("LOAD_WRITER_TARGET_INVALID")
        inspector = PostgreSQLDatabaseAdapter(target.inspection)._target
        self._writer = PostgreSQLWriterTarget(
            inspection=inspector, dsn=target.dsn, principal=target.principal
        )
        self._policy = checked(policy, PostgreSQLLoadPolicy, 4194304)
        if target.principal != policy.preflight.writer_principal:
            raise failure("LOAD_WRITER_TARGET_INVALID")
        self._staging = staging
        self._clock = clock or (lambda: datetime.now(UTC))
        self._active = threading.Lock()
        self._unavailable = False

    def _now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is not UTC:
            raise failure("LOAD_CLOCK_INVALID")
        return value

    async def dry_run(self, request: DryRunRequest) -> DryRunExecutionPlan:
        """Построить прогноз через inspector без writer, staging или audit I/O.

        Args:
            request: Полный normalized snapshot и соответствующий MappingPlan.

        Returns:
            Ordered execution plan с target-unit counts и blockers текущего snapshot.

        Raises:
            DatabaseInspectionError: Inspector недоступен для нового вызова.
            LoadError: Неверный вход, scope, permissions, schema drift или DB/budget error.
            asyncio.CancelledError: Отмена после ограниченного rollback/close.

        Использует SERIALIZABLE READ ONLY transaction. Server-value permissions
        writer не позволяют вычислять defaults/generated values во время прогноза.
        Для logs предназначен только safe_summary результата.
        """
        return await PostgreSQLDryRunPlanner(
            self._writer.inspection, policy=self._policy.preflight
        ).plan(request)

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        """Проверить staging, повторить M11/M12 в writer transaction и загрузить.

        Args:
            request: Snapshot, полный staging context, SEALED revision и key,
                обязательный только при включённом ledger.

        Returns:
            PostgreSQLLoadResult после подтверждённого COMMIT или ledger replay.
            Counts относятся к target units; source counts указаны отдельно.

        Raises:
            LoadError: Нарушены scope/lineage, grants, constraints или budgets.
                До COMMIT target writes откатываются. LOAD_OUTCOME_UNKNOWN означает,
                что COMMIT нельзя подтвердить; он не доказывает rollback.
            asyncio.CancelledError: Отмена до начала COMMIT после rollback/cleanup.

        Меняет target DML и staging lifecycle без DDL. Atomic — default; quarantine
        требует отдельную policy/ledger и откатывает связанные группы SAVEPOINT.
        Schema fingerprint проверяется перед DML и COMMIT. Values параметризованы,
        identifiers получены из validated catalog/plan, generated columns не пишутся.

        После подтверждённого COMMIT ошибки finalize/cleanup дают result с warning.
        При UNKNOWN ledger позволяет повторно проверить marker того же key/binding
        без повторного DML. Без ledger экземпляр блокируется до внешнего reconcile.
        Отсутствие marker не разрешает писать из EXECUTING/UNKNOWN staging.
        Replay сохраняет исходные counts/run_id с replayed=True; safe_summary
        показывает новые INSERT/UPDATE равными нулю. Полный JSON чувствителен.
        """
        try:
            if self._policy.ledger is not None:
                # Между processes и вызовами одного экземпляра authority — DB lock.
                return await self._execute(request)
            with inspection_slot(self._active, unavailable=self._unavailable):
                return await self._execute(request)
        except StructuraGuardError as error:
            code = error.error_code
        except TimeoutError:
            code = "PROCESSING_TIMEOUT"
        raise failure(code) from None

    async def _execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        policy = self._policy
        max_bytes = policy.preflight.read_policy.limits.max_bytes
        request = checked(request, LoadRequest, max_bytes)
        if (policy.ledger is None) != (request.idempotency_key is None):
            raise failure("LOAD_IDEMPOTENCY_REQUIRED")
        target = writer_scope(self._writer)
        started_at = self._now()
        budget_end = asyncio.get_running_loop().time() + target.limits.timeout_seconds
        deadline = started_at + timedelta(seconds=target.limits.timeout_seconds)
        claimed: StagingRun | None = None
        async with asyncio.timeout(target.limits.timeout_seconds):
            prepared = await prepare(
                request.snapshot,
                max_bytes=max_bytes,
                max_records=policy.preflight.read_policy.limits.max_records,
            )

        async def claim() -> StagingRun:
            nonlocal claimed
            if claimed is not None:
                return claimed
            run = await verify_staging(
                self._staging, request, prepared, deadline=deadline, max_bytes=max_bytes
            )
            claimed = checked(
                await self._staging.transition(
                    request.staging_context,
                    expected_revision=run.revision,
                    status=StagingRunStatus.EXECUTING,
                ),
                StagingRun,
                max_bytes,
            )
            if (
                claimed.spec != run.spec
                or claimed.sealed_fingerprint != run.sealed_fingerprint
                or claimed.status is not StagingRunStatus.EXECUTING
                or claimed.revision != run.revision + 1
            ):
                raise failure("LOAD_STAGING_BINDING_MISMATCH")
            return claimed

        if policy.ledger is None:
            async with asyncio.timeout(
                max(0.001, budget_end - asyncio.get_running_loop().time())
            ):
                await claim()

        def check_deadline() -> None:
            if asyncio.get_running_loop().time() >= budget_end:
                raise failure("PROCESSING_TIMEOUT")
            if self._now() >= request.staging_context.expires_at:
                raise failure("LOAD_STAGING_EXPIRED")

        outcome = await execute_transaction(
            target,
            prepared,
            policy,
            request=request,
            started_at=started_at,
            claim_staging=claim,
            check_deadline=check_deadline,
            timeout_seconds=max(0.001, budget_end - asyncio.get_running_loop().time()),
        )
        result = outcome.result
        finalize_status = (
            StagingRunStatus.QUARANTINED
            if result is not None and result.quarantined
            else outcome.status
        )
        finalized, finalize_cancelled = True, False
        if claimed is not None:
            finalized, finalize_cancelled = await finish_staging(
                self._staging, claimed, finalize_status, target.limits.cleanup_seconds
            )
        elif (
            result is not None
            and result.replayed
            and result.run_id == request.staging_context.run_id
        ):
            finalized, finalize_cancelled = await self._recover_staging(
                request, result, target.limits.cleanup_seconds
            )
        if outcome.status is StagingRunStatus.UNKNOWN:
            self._unavailable = policy.ledger is None
            raise failure("LOAD_OUTCOME_UNKNOWN")
        if outcome.status is not StagingRunStatus.COMMITTED:
            if outcome.cancelled or finalize_cancelled:
                raise asyncio.CancelledError
            raise failure(outcome.code or "LOAD_DATABASE_FAILED")
        assert result is not None
        warnings = outcome.warnings
        if not finalized:
            warnings += ("LOAD_STAGING_FINALIZE_FAILED",)
        if finalize_cancelled:
            warnings += ("LOAD_CANCELLED_AFTER_COMMIT",)
        return result.model_copy(
            update={"warnings": tuple(dict.fromkeys((*result.warnings, *warnings)))}
        )

    async def _recover_staging(
        self, request: LoadRequest, result: PostgreSQLLoadResult, seconds: float
    ) -> tuple[bool, bool]:
        try:
            async with asyncio.timeout(seconds):
                run = await self._staging.get_run(request.staging_context)
                if run.sealed_fingerprint != result.sealed_fingerprint:
                    return False, False
                if run.status not in (
                    StagingRunStatus.EXECUTING,
                    StagingRunStatus.UNKNOWN,
                ):
                    return True, False
        except asyncio.CancelledError:
            return False, True
        except (
            StructuraGuardError,
            TimeoutError,
            ValueError,
            TypeError,
            OSError,
            RuntimeError,
        ):
            return False, False
        return await finish_staging(
            self._staging,
            run,
            StagingRunStatus.QUARANTINED
            if result.quarantined
            else StagingRunStatus.COMMITTED,
            seconds,
        )
