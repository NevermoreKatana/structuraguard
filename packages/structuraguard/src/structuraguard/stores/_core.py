"""Общие детерминированные правила lifecycle; persistence выполняет adapter."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ValidationError

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import UtcDateTime
from structuraguard.contracts.database import StagingContext
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.staging import (
    StagedBatch,
    StagedRecord,
    StagingLimits,
    StagingRetentionPolicy,
    StagingRun,
    StagingRunSpec,
    StagingRunStatus,
)
from structuraguard.exceptions import StagingError

type Access = Literal["read", "write", "cleanup"]


def failure(code: str) -> StagingError:
    return StagingError(error_code=code, message=code)


def _later(now: datetime, seconds: int) -> datetime:
    try:
        return now + timedelta(seconds=seconds)
    except OverflowError:
        raise failure("STAGING_INPUT_INVALID") from None


def bounded(value: object, limits: StagingLimits) -> None:
    """Ограничить traversal до dump/hash, включая forged DTO и циклы."""
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        size += 16
        if nodes > limits.max_nodes or depth > 64 or size > limits.max_input_bytes:
            raise failure("STAGING_LIMIT_EXCEEDED")
        children: tuple[object, ...] = ()
        if isinstance(item, (str, bytes)):
            size += len(item) * 4
        elif isinstance(item, BaseModel):
            children = tuple(item.__dict__.values())
        elif isinstance(item, Mapping):
            if len(item) > limits.max_nodes:
                raise failure("STAGING_LIMIT_EXCEEDED")
            children = (*item.keys(), *item.values())
        elif isinstance(item, (tuple, list)):
            if len(item) > limits.max_nodes:
                raise failure("STAGING_LIMIT_EXCEEDED")
            children = tuple(item)
        elif isinstance(item, int):
            if item.bit_length() > 4096:
                raise failure("STAGING_LIMIT_EXCEEDED")
        elif isinstance(item, Decimal):
            parts = item.as_tuple()
            if (
                not item.is_finite()
                or len(parts.digits) > 1000
                or not isinstance(parts.exponent, int)
                or abs(parts.exponent) > 10000
            ):
                raise failure("STAGING_LIMIT_EXCEEDED")
        elif item is not None and not isinstance(item, (date, float)):
            raise failure("STAGING_INPUT_INVALID")
        if len(pending) + len(children) + nodes > limits.max_nodes:
            raise failure("STAGING_LIMIT_EXCEEDED")
        pending.extend((child, depth + 1) for child in children)
    if size > limits.max_input_bytes:
        raise failure("STAGING_LIMIT_EXCEEDED")


def checked[T: FrozenContract](value: T, cls: type[T], limits: StagingLimits) -> T:
    if type(value) is not cls:
        raise failure("STAGING_INPUT_INVALID")
    bounded(value, limits)
    try:
        return cls.model_validate(value.model_dump(mode="python", warnings=False))
    except (ValidationError, ValueError, TypeError):
        raise failure("STAGING_INPUT_INVALID") from None


class _Time(FrozenContract):
    value: UtcDateTime


@dataclass(frozen=True)
class State:
    run: StagingRun
    batches: tuple[StagedBatch, ...] = ()
    records: tuple[StagedRecord, ...] = ()


class StagingOperations(ABC):
    """Lifecycle на атомарном read-modify-write backend без произвольного SQL."""

    def __init__(
        self,
        *,
        target_id: str,
        retention: StagingRetentionPolicy,
        limits: StagingLimits | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._limits = checked(
            limits or StagingLimits(), StagingLimits, StagingLimits()
        )
        self._retention = checked(retention, StagingRetentionPolicy, self._limits)
        self._target_id = target_id
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        try:
            return _Time(value=self._clock()).value
        except (ValidationError, ValueError, TypeError):
            raise failure("STAGING_CLOCK_INVALID") from None

    def _context(self, context: StagingContext) -> StagingContext:
        context = checked(context, StagingContext, self._limits)
        if context.target_id != self._target_id:
            raise failure("STAGING_CONTEXT_MISMATCH")
        return context

    def _state(self, state: State | None, context: StagingContext) -> State:
        if state is None:
            raise failure("STAGING_RUN_NOT_FOUND")
        if state.run.spec.context != context:
            raise failure("STAGING_CONTEXT_MISMATCH")
        if state.run.retention_fingerprint != self._retention.fingerprint:
            raise failure("STAGING_RETENTION_MISMATCH")
        return state

    @abstractmethod
    async def _access[T](
        self,
        context: StagingContext,
        mode: Access,
        operation: Callable[[State | None], tuple[State, T]],
    ) -> T: ...

    async def begin(self, spec: StagingRunSpec) -> StagingRun:
        """Создать run без DDL; повтор полного spec возвращает прежнюю metadata.

        Args:
            spec: Полный context, fingerprints, ожидаемые counts и удерживаемые refs.

        Returns:
            Созданный OPEN run либо прежний run с тем же spec до purge.

        Raises:
            StagingError: Неверные scope, retention, expiry, budget или persistence;
                другой spec того же run даёт STAGING_RUN_EXISTS.
            asyncio.CancelledError: Отмена после освобождения ресурсов backend.

        Сохраняются только metadata и refs; внешние артефакты не открываются.
        PostgreSQL выполняет отдельную transaction; схема должна существовать.
        """
        spec = checked(spec, StagingRunSpec, self._limits)
        context = self._context(spec.context)
        if (
            spec.record_count > self._limits.max_records
            or spec.batch_count > self._limits.max_batches
        ):
            raise failure("STAGING_LIMIT_EXCEEDED")
        if {r.kind for r in spec.references} != set(self._retention.retain_kinds):
            raise failure("STAGING_RETENTION_MISMATCH")
        keep_until = _later(
            context.expires_at,
            max(self._retention.success_seconds, self._retention.failure_seconds),
        )
        if any(r.retained_until < keep_until for r in spec.references):
            raise failure("STAGING_REFERENCE_EXPIRES")
        fingerprint = canonical_sha256_value(spec)

        def apply(state: State | None) -> tuple[State, StagingRun]:
            now = self._now()
            if state is not None:
                self._state(state, context)
                if state.run.spec_fingerprint != fingerprint or state.run.purged:
                    raise failure("STAGING_RUN_EXISTS")
                return state, state.run
            if (
                not now
                < context.expires_at
                <= _later(now, self._retention.max_run_seconds)
            ):
                raise failure("STAGING_EXPIRED")
            run = StagingRun(
                spec=spec,
                spec_fingerprint=fingerprint,
                retention_fingerprint=self._retention.fingerprint,
                created_at=now,
                updated_at=now,
            )
            return State(run), run

        return await self._access(context, "write", apply)

    async def get_run(self, context: StagingContext) -> StagingRun:
        """Прочитать metadata run, включая сохранённый после cleanup tombstone.

        Args:
            context: Весь исходный StagingContext, включая scope и fingerprints.

        Returns:
            Неизменяемый StagingRun с текущей revision и lifecycle status.

        Raises:
            StagingError: Run не найден, context/policy не совпали либо backend недоступен.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет run и не открывает artifacts. Полный JSON metadata чувствителен.
        """
        context = self._context(context)

        def read(state: State | None) -> tuple[State, StagingRun]:
            state = self._state(state, context)
            return state, state.run

        return await self._access(context, "read", read)

    async def stage(self, batch: NormalizedBatch, context: StagingContext) -> None:
        """Атомарно сохранить batch/record metadata и ссылки, заданные begin.

        Args:
            batch: Следующий NormalizedBatch 1.1/1.2 с проверяемым fingerprint.
            context: Полный context ранее открытого run.

        Returns:
            None после записи metadata; идентичный batch в OPEN run — no-op.

        Raises:
            StagingError: Нарушены порядок, hashes, counts, limits, срок или состояние;
                при persistence failure результат определяется закрытым error_code.
            asyncio.CancelledError: Отмена после cleanup backend.

        Scalar payload не копируется. Summary, record index и counts сохраняются
        вместе; иной content того же ordinal запрещён. Запись в target не разрешается.
        """
        context = self._context(context)
        batch = checked(batch, NormalizedBatch, self._limits)
        if batch.schema_version == "1.0.0":
            raise failure("STAGING_INPUT_INVALID")
        if len(batch.records) > self._limits.max_batch_records:
            raise failure("STAGING_LIMIT_EXCEEDED")

        def apply(current: State | None) -> tuple[State, None]:
            state = self._state(current, context)
            run, spec = state.run, state.run.spec
            now = self._now()
            if run.status is not StagingRunStatus.OPEN:
                raise failure("STAGING_STATE_INVALID")
            if now >= context.expires_at:
                raise failure("STAGING_EXPIRED")
            if (
                batch.source.source_fingerprint,
                batch.extraction_fingerprint,
                batch.parse_plan_fingerprint,
            ) != (
                spec.source_fingerprint,
                spec.extraction_fingerprint,
                spec.parse_plan_fingerprint,
            ):
                raise failure("STAGING_CONTEXT_MISMATCH")
            if batch.batch_index < len(state.batches):
                if state.batches[batch.batch_index].summary != batch.to_summary():
                    raise failure("STAGING_CONTENT_MISMATCH")
                return state, None
            if (
                batch.batch_index != len(state.batches)
                or batch.batch_index >= spec.batch_count
            ):
                raise failure("STAGING_BATCH_ORDER_INVALID")
            if batch.is_last != (batch.batch_index == spec.batch_count - 1):
                raise failure("STAGING_INCOMPLETE")
            if run.record_count + len(batch.records) > spec.record_count:
                raise failure("STAGING_LIMIT_EXCEEDED")
            existing = {r.record_id for r in state.records}
            if any(r.record_id in existing for r in batch.records):
                raise failure("STAGING_RECORD_DUPLICATE")
            summaries = (
                *state.batches,
                StagedBatch(summary=batch.to_summary(), created_at=now),
            )
            if batch.manifest is not None and (
                batch.manifest.normalized_fingerprint != context.normalized_fingerprint
                or batch.manifest.batches != tuple(b.summary for b in summaries)
                or batch.manifest.record_count != spec.record_count
            ):
                raise failure("STAGING_CONTEXT_MISMATCH")
            records = tuple(
                StagedRecord(
                    record_id=r.record_id,
                    record_index=run.record_count + index,
                    batch_index=batch.batch_index,
                    index_in_batch=index,
                    record_fingerprint=canonical_sha256_value(r),
                    references=spec.references,
                    created_at=now,
                )
                for index, r in enumerate(batch.records)
            )
            updated = run.model_copy(
                update={
                    "revision": run.revision + 1,
                    "batch_count": run.batch_count + 1,
                    "record_count": run.record_count + len(records),
                    "updated_at": now,
                }
            )
            result = State(updated, summaries, (*state.records, *records))
            bounded((updated, result.batches, result.records), self._limits)
            return result, None

        await self._access(context, "write", apply)

    def _revision(self, run: StagingRun, expected: int) -> None:
        if type(expected) is not int or run.revision != expected:
            raise failure("STAGING_REVISION_CONFLICT")

    async def seal(
        self, context: StagingContext, *, expected_revision: int
    ) -> StagingRun:
        """Закрыть полный staged snapshot для дальнейшего append через CAS.

        Args:
            context: Полный context существующего OPEN run.
            expected_revision: Точная revision, полученная через get_run.

        Returns:
            SEALED run с новой revision и sealed_fingerprint.

        Raises:
            StagingError: Не совпали revision/counts, истёк срок, неверны state/backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Меняет только metadata. Caller завершает внешний iterator до seal;
        проверки terminal manifest в stage не заменяют M11/M12 и physical replay.
        """
        context = self._context(context)

        def apply(current: State | None) -> tuple[State, StagingRun]:
            state = self._state(current, context)
            run = state.run
            self._revision(run, expected_revision)
            if run.status is not StagingRunStatus.OPEN:
                raise failure("STAGING_STATE_INVALID")
            if self._now() >= context.expires_at:
                raise failure("STAGING_EXPIRED")
            if (run.batch_count, run.record_count) != (
                run.spec.batch_count,
                run.spec.record_count,
            ):
                raise failure("STAGING_INCOMPLETE")
            fingerprint = canonical_sha256_value(
                (
                    run.spec_fingerprint,
                    tuple(b.summary.canonical_json() for b in state.batches),
                    tuple(r.record_fingerprint for r in state.records),
                )
            )
            updated = run.model_copy(
                update={
                    "status": StagingRunStatus.SEALED,
                    "revision": run.revision + 1,
                    "updated_at": self._now(),
                    "sealed_fingerprint": fingerprint,
                }
            )
            return replace(state, run=updated), updated

        return await self._access(context, "write", apply)

    async def read_records(
        self, context: StagingContext, *, offset: int, limit: int
    ) -> tuple[StagedRecord, ...]:
        """Прочитать ограниченную страницу metadata закрытого для append run.

        Args:
            context: Полный context run.
            offset: Неотрицательное смещение в record index.
            limit: Размер страницы от 1 до limits.max_page_records.

        Returns:
            Tuple StagedRecord в исходном порядке; за концом index — пустой tuple.

        Raises:
            StagingError: OPEN/purged run, истёкшие refs/retention, неверные context,
                пределы страницы либо ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет metadata и не открывает refs. PostgreSQL проверяет весь bounded
        run при каждом чтении; размер страницы не ограничивает стоимость проверки.
        """
        context = self._context(context)
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= self._limits.max_page_records
        ):
            raise failure("STAGING_LIMIT_EXCEEDED")

        def read(current: State | None) -> tuple[State, tuple[StagedRecord, ...]]:
            state = self._state(current, context)
            if state.run.purged:
                raise failure("STAGING_PURGED")
            if state.run.status is StagingRunStatus.OPEN:
                raise failure("STAGING_STATE_INVALID")
            if (
                state.run.cleanup_after is not None
                and self._now() >= state.run.cleanup_after
            ):
                raise failure("STAGING_EXPIRED")
            if any(r.retained_until <= self._now() for r in state.run.spec.references):
                raise failure("STAGING_REFERENCE_EXPIRES")
            return state, state.records[offset : offset + limit]

        return await self._access(context, "read", read)

    async def read_batches(
        self, context: StagingContext, *, offset: int, limit: int
    ) -> tuple[StagedBatch, ...]:
        """Прочитать страницу batch summaries, в том числе для OPEN run.

        Args:
            context: Полный context run.
            offset: Неотрицательное смещение в batch index.
            limit: Размер страницы от 1 до limits.max_page_records.

        Returns:
            Tuple StagedBatch в порядке batches; за концом index — пустой tuple.

        Raises:
            StagingError: Purge, истёкшая retention, неверный context, размер или backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Не меняет metadata и не читает payload. Стоимость PostgreSQL проверки
        пропорциональна всему bounded run, даже для короткой страницы.
        """
        context = self._context(context)
        if (
            type(offset) is not int
            or type(limit) is not int
            or offset < 0
            or not 1 <= limit <= self._limits.max_page_records
        ):
            raise failure("STAGING_LIMIT_EXCEEDED")

        def read(current: State | None) -> tuple[State, tuple[StagedBatch, ...]]:
            state = self._state(current, context)
            if state.run.purged:
                raise failure("STAGING_PURGED")
            if (
                state.run.cleanup_after is not None
                and self._now() >= state.run.cleanup_after
            ):
                raise failure("STAGING_EXPIRED")
            return state, state.batches[offset : offset + limit]

        return await self._access(context, "read", read)

    async def transition(
        self,
        context: StagingContext,
        *,
        expected_revision: int,
        status: StagingRunStatus,
    ) -> StagingRun:
        """Записать разрешённый lifecycle transition с проверкой revision.

        Args:
            context: Полный context run.
            expected_revision: Текущая revision для CAS.
            status: Разрешённое следующее состояние StagingRunStatus.

        Returns:
            Run с новым status, revision и рассчитанным cleanup_after.

        Raises:
            StagingError: Конфликт revision, запрещённый переход или ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Меняет metadata; caller отвечает за истинность объявленного DB outcome.
        UNKNOWN не истекает автоматически. Terminal run не возвращается в OPEN.
        """
        context = self._context(context)
        if not isinstance(status, StagingRunStatus):
            raise failure("STAGING_STATE_INVALID")

        def apply(current: State | None) -> tuple[State, StagingRun]:
            state = self._state(current, context)
            run = state.run
            self._revision(run, expected_revision)
            allowed = {
                StagingRunStatus.OPEN: (
                    StagingRunStatus.FAILED,
                    StagingRunStatus.CANCELLED,
                ),
                StagingRunStatus.SEALED: (
                    StagingRunStatus.EXECUTING,
                    StagingRunStatus.FAILED,
                    StagingRunStatus.CANCELLED,
                ),
                StagingRunStatus.EXECUTING: (
                    StagingRunStatus.COMMITTED,
                    StagingRunStatus.QUARANTINED,
                    StagingRunStatus.ROLLED_BACK,
                    StagingRunStatus.FAILED,
                    StagingRunStatus.CANCELLED,
                    StagingRunStatus.UNKNOWN,
                ),
                StagingRunStatus.UNKNOWN: (
                    StagingRunStatus.COMMITTED,
                    StagingRunStatus.QUARANTINED,
                    StagingRunStatus.ROLLED_BACK,
                ),
            }
            if status not in allowed.get(run.status, ()) or run.purged:
                raise failure("STAGING_STATE_INVALID")
            now = self._now()
            if status is StagingRunStatus.EXECUTING and now >= context.expires_at:
                raise failure("STAGING_EXPIRED")
            cleanup_after = None
            if status not in (StagingRunStatus.EXECUTING, StagingRunStatus.UNKNOWN):
                seconds = (
                    self._retention.success_seconds
                    if status is StagingRunStatus.COMMITTED
                    else self._retention.failure_seconds
                )
                cleanup_after = _later(now, seconds)
            updated = run.model_copy(
                update={
                    "status": status,
                    "revision": run.revision + 1,
                    "updated_at": now,
                    "cleanup_after": cleanup_after,
                }
            )
            return replace(state, run=updated), updated

        return await self._access(context, "write", apply)

    async def cleanup(self, context: StagingContext) -> StagingRun:
        """Применить retention к одному run, сохранив tombstone и его идентичность.

        Args:
            context: Полный context run; PostgreSQL требует maintenance target.

        Returns:
            Текущий run либо очищенный tombstone с purged=True без refs и index.

        Raises:
            StagingError: Неверный context, запрещённая роль или ошибка backend.
            asyncio.CancelledError: Отмена после cleanup backend.

        Истёкший OPEN/SEALED получает EXPIRED; EXECUTING/UNKNOWN сохраняются.
        После retention удаляются batch/record metadata и refs. Внешние artifacts
        и loader ledger не удаляются; run_id остаётся занят.
        """
        context = self._context(context)

        def apply(current: State | None) -> tuple[State, StagingRun]:
            state = self._state(current, context)
            run = state.run
            if (
                run.status in (StagingRunStatus.OPEN, StagingRunStatus.SEALED)
                and self._now() >= context.expires_at
            ):
                run = run.model_copy(
                    update={
                        "status": StagingRunStatus.EXPIRED,
                        "revision": run.revision + 1,
                        "updated_at": self._now(),
                        "cleanup_after": _later(
                            context.expires_at, self._retention.failure_seconds
                        ),
                    }
                )
                state = replace(state, run=run)
            if (
                run.purged
                or run.cleanup_after is None
                or self._now() < run.cleanup_after
            ):
                return state, run
            updated = run.model_copy(
                update={
                    "spec": run.spec.model_copy(update={"references": ()}),
                    "purged": True,
                    "revision": run.revision + 1,
                    "updated_at": self._now(),
                }
            )
            return State(updated), updated

        return await self._access(context, "cleanup", apply)
