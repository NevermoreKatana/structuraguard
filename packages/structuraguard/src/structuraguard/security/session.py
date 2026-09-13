"""Run-local accounting и явные resource boundaries без глобального состояния."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Never
from uuid import UUID

from structuraguard.contracts.security import (
    Resource,
    ResourceAuditEvent,
    ResourceErrorCode,
    SecurityLimits,
    SecurityPolicy,
)
from structuraguard.contracts.source import ExtractedBatch, SourceArtifact
from structuraguard.exceptions import SecurityPolicyError, StructuraGuardError
from structuraguard.parsers import ParserRegistry
from structuraguard.ports.parser import Parser
from structuraguard.ports.resources import SourceStream
from structuraguard.ports.source import ParseContext, ProbeContext, SourceReader

from .source import BoundedSnapshot

if TYPE_CHECKING:
    from structuraguard.contracts.llm import LLMRoutingPolicy
    from structuraguard.contracts.loading import PostgreSQLLoadPolicy
    from structuraguard.database.target import InspectionLimits


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _invalid_policy() -> SecurityPolicyError:
    return SecurityPolicyError(
        error_code="SECURITY_POLICY_INVALID", message="Некорректная resource policy."
    )


def _checked_policy(policy: SecurityPolicy) -> SecurityPolicy:
    # Не запускать serializer на forged вложенных значениях или чужом DTO.
    if type(policy) is not SecurityPolicy or type(policy.limits) is not SecurityLimits:
        raise _invalid_policy()
    if (
        any(
            type(getattr(policy.limits, name, None)) is not int
            for name in SecurityLimits.model_fields
        )
        or type(policy.allowed_formats) is not tuple
    ):
        raise _invalid_policy()

    def bounded_format(value: object) -> bool:
        return type(value) is str and len(value) <= 16

    if (
        len(policy.allowed_formats) > 13
        or not all(bounded_format(value) for value in policy.allowed_formats)
        or not bounded_format(policy.parser_trust)
    ):
        raise _invalid_policy()
    try:
        return SecurityPolicy.model_validate(policy.model_dump(warnings="error"))
    except (ValueError, TypeError):
        raise _invalid_policy() from None


class SecuritySession:
    """Один источник/run и общий максимум для parser, LLM и DB.

    Args:
        policy: Неизменяемая trusted policy; narrowing выполняется явно до run.
        run_id: UUID от host, не произвольный пользовательский identifier.
        clock: UTC часы для безопасного audit event.
        monotonic: Монотонные секунды для общего и stage deadlines.

    Конструктор начинает общий deadline, но не открывает files/network/DB.
    Первый отказ/отмена закрывает session; events хранит максимум одно terminal
    событие без raw данных. Audit I/O принадлежит host. Бюджет распространяется
    только на adapters, получившие эту session через resources.

    Raises:
        SecurityPolicyError: SECURITY_POLICY_INVALID для неверной конфигурации;
            методы также дают SECURITY_LIMIT_EXCEEDED, PROCESSING_TIMEOUT или
            SECURITY_RUN_CLOSED. Уже выданные batches промежуточные до EOF."""

    def __init__(
        self,
        policy: SecurityPolicy,
        *,
        run_id: UUID,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = _checked_policy(policy)
        if type(run_id) is not UUID:
            raise _invalid_policy()
        self._run_id, self._clock, self._monotonic = run_id, clock, monotonic
        self._start = monotonic()
        if type(self._start) not in (int, float) or not math.isfinite(self._start):
            raise _invalid_policy()
        self._last_tick = self._start
        self._stage_starts: dict[Resource, float] = {}
        self._counts: dict[Resource, int] = {}
        self._event: ResourceAuditEvent | None = None
        self._lock = threading.RLock()
        self._parser_used = False
        self._snapshot_used = False

    @property
    def policy(self) -> SecurityPolicy:
        """Проверенный frozen snapshot настроек текущего run."""
        return self._policy

    @property
    def run_id(self) -> UUID:
        """Opaque identity для связывания runner и audit evidence."""
        return self._run_id

    @property
    def limits(self) -> SecurityLimits:
        """Проверенные maxima для adapter resource protocols."""
        return self._policy.limits

    @property
    def events(self) -> tuple[ResourceAuditEvent, ...]:
        """Bounded audit evidence без source/SQL/credentials/raw exception text."""
        with self._lock:
            return (self._event,) if self._event is not None else ()

    def used(self, resource: Resource) -> int:
        """Принятый счётчик; отклонённая reservation его не изменяет."""
        with self._lock:
            return self._counts.get(resource, 0)

    def _record(
        self,
        code: ResourceErrorCode,
        resource: Resource | None = None,
        *,
        limit: int | None = None,
        observed: int | None = None,
    ) -> None:
        if self._event is not None:
            return
        self._event = ResourceAuditEvent(
            run_id=str(self._run_id),
            policy_fingerprint=self._policy.fingerprint,
            occurred_at=self._clock(),
            outcome="cancelled"
            if code == "SECURITY_OPERATION_CANCELLED"
            else "failed"
            if code in {"PROCESSING_TIMEOUT", "SECURITY_OPERATION_FAILED"}
            else "rejected",
            code=code,
            resource=resource,
            limit=limit,
            observed=observed,
        )

    def deny(
        self,
        code: ResourceErrorCode = "SECURITY_INPUT_REJECTED",
        resource: Resource | None = None,
        *,
        limit: int | None = None,
        observed: int | None = None,
    ) -> Never:
        """Закрыть run и выдать typed security error с безопасным event."""
        with self._lock:
            self._record(code, resource, limit=limit, observed=observed)
            raise SecurityPolicyError(
                error_code=code,
                message="Операция отклонена resource policy.",
                details={
                    "resource": resource.value if resource else "operation",
                    "limit": limit,
                    "observed": observed,
                },
                run_id=f"run-{self._run_id}",
            ) from None

    def record_cancelled(self) -> None:
        """Закрыть run безопасным event без текста CancelledError."""
        with self._lock:
            self._record("SECURITY_OPERATION_CANCELLED")

    def record_failure(self, code: str) -> None:
        """Сохранить terminal resource error после transactional cleanup."""
        known: ResourceErrorCode = "SECURITY_OPERATION_FAILED"
        if type(code) is str and len(code) <= 64:
            if code == "SECURITY_LIMIT_EXCEEDED":
                known = "SECURITY_LIMIT_EXCEEDED"
            elif code == "PROCESSING_TIMEOUT":
                known = "PROCESSING_TIMEOUT"
        with self._lock:
            self._record(known)

    def _active(self) -> None:
        if self._event is not None:
            raise SecurityPolicyError(
                error_code="SECURITY_RUN_CLOSED", message="Resource run уже завершён."
            )

    def _tick(self) -> float:
        now = self._monotonic()
        if (
            type(now) not in (int, float)
            or not math.isfinite(now)
            or now < self._last_tick
        ):
            self.deny("SECURITY_POLICY_INVALID")
        self._last_tick = now
        return now

    def _check(self, resource: Resource, value: int) -> None:
        self._active()
        if (
            type(resource) is not Resource
            or type(value) is not int
            or not 0 <= value < 2**63
        ):
            self.deny("SECURITY_POLICY_INVALID")
        maximum = self.policy.limits.maximum(resource)
        if value > maximum:
            self.deny(
                "SECURITY_LIMIT_EXCEEDED", resource, limit=maximum, observed=value
            )

    def check(self, resource: Resource, value: int) -> None:
        """Проверить размер/ширину/depth до добавления объекта в буфер."""
        with self._lock:
            self._check(resource, value)

    def reserve(self, resource: Resource, amount: int) -> None:
        """Атомарно учесть количество до side effect, без refund после ошибки."""
        with self._lock:
            if type(amount) is not int or not 0 <= amount < 2**63:
                self.deny("SECURITY_POLICY_INVALID")
            value = self.used(resource) + amount
            self._check(resource, value)
            self._counts[resource] = value

    def remaining_seconds(self, resource: Resource) -> float:
        """Оставшийся общий/stage deadline; истечение не допускает новый await."""
        with self._lock:
            self._active()
            if resource not in {
                Resource.PARSER_TIME_MS,
                Resource.LLM_TIME_MS,
                Resource.PROCESSING_TIME_MS,
            }:
                self.deny("SECURITY_POLICY_INVALID")
            now = self._tick()
            start = self._stage_starts.setdefault(resource, now)
            overall = self.policy.limits.max_processing_time_ms / 1000 - (
                now - self._start
            )
            remaining = self.policy.limits.maximum(resource) / 1000 - (now - start)
            if min(overall, remaining) <= 0:
                expired = (
                    resource if remaining <= overall else Resource.PROCESSING_TIME_MS
                )
                self.deny(
                    "PROCESSING_TIMEOUT",
                    expired,
                    limit=self.policy.limits.maximum(expired),
                )
            return min(overall, remaining)

    async def call[T](
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        resource: Resource = Resource.PROCESSING_TIME_MS,
    ) -> T:
        """Выполнить trusted adapter await с deadline и cancellation propagation.

        operation создаётся только после preflight. Не принимает/исполняет SQL,
        source code или LLM output. Cleanup operation остаётся у adapter.
        Только read-only или отменяемые операции: loader.execute подключает
        resources напрямую, чтобы post-return deadline не исказил COMMIT outcome.
        """
        remaining = self.remaining_seconds(resource)
        try:
            async with asyncio.timeout(remaining):
                result = await operation()
            self.remaining_seconds(resource)
            return result
        except asyncio.CancelledError:
            with self._lock:
                self._record("SECURITY_OPERATION_CANCELLED", resource)
            raise asyncio.CancelledError from None
        except TimeoutError:
            self.remaining_seconds(resource)
            self.deny(
                "PROCESSING_TIMEOUT",
                resource,
                limit=self.policy.limits.maximum(resource),
            )
        except StructuraGuardError as error:
            if self.events:
                # Сохранить исходное safe решение даже если adapter заменил error.
                event = self.events[0]
                self.deny(
                    event.code,
                    event.resource,
                    limit=event.limit,
                    observed=event.observed,
                )
            if error.error_code in {"SECURITY_LIMIT_EXCEEDED", "LLM_BUDGET_EXCEEDED"}:
                names = {r.value: r for r in Resource}
                names.update(
                    source_bytes=Resource.FILE_BYTES,
                    batch_count=Resource.CHUNKS,
                    batches=Resource.CHUNKS,
                    line_count=Resource.RECORDS,
                )
                name = error.details.get("resource")
                bound = error.details.get("limit")
                known = (
                    names.get(name) if type(name) is str and len(name) <= 64 else None
                )
                self.deny(
                    "SECURITY_LIMIT_EXCEEDED",
                    known,
                    limit=bound if type(bound) is int and 0 <= bound < 2**63 else None,
                )
            if error.error_code in {"PROCESSING_TIMEOUT", "LLM_TIMEOUT"}:
                self.deny("PROCESSING_TIMEOUT", resource)
            if isinstance(error, SecurityPolicyError) or error.error_code in {
                "PARSER_FORMAT_UNSUPPORTED",
                "PARSER_UNSUPPORTED_FORMAT",
                "PARSER_FORMAT_CONFLICT",
                "LLM_POLICY_DENIED",
            }:
                self.deny("SECURITY_INPUT_REJECTED")
            with self._lock:
                self._record("SECURITY_OPERATION_FAILED")
            raise
        except (ValueError, TypeError, AttributeError, RuntimeError, OSError):
            self.deny("SECURITY_OPERATION_FAILED")

    def before_query(self) -> None:
        """Общий budget всех подключённых DB adapters, до driver invocation."""
        self.remaining_seconds(Resource.PROCESSING_TIME_MS)
        self.reserve(Resource.DB_QUERIES, 1)

    def check_deadline(self) -> None:
        """Проверить общий deadline до COMMIT без расходования query budget.

        Истечение закрывает run с SecurityPolicyError/PROCESSING_TIMEOUT.
        Подтверждённый COMMIT этой проверкой нельзя объявить откатившимся."""
        self.remaining_seconds(Resource.PROCESSING_TIME_MS)

    def reserve_llm(self, tokens: int) -> None:
        """Атомарный общий резерв нескольких router/provider instances одного run."""
        with self._lock:
            self.remaining_seconds(Resource.LLM_TIME_MS)
            self._check(Resource.LLM_CALLS, self.used(Resource.LLM_CALLS) + 1)
            if type(tokens) is not int or tokens <= 0:
                self.deny("SECURITY_POLICY_INVALID")
            self._check(Resource.LLM_TOKENS, self.used(Resource.LLM_TOKENS) + tokens)
            self._counts[Resource.LLM_CALLS] = self.used(Resource.LLM_CALLS) + 1
            self._counts[Resource.LLM_TOKENS] = self.used(Resource.LLM_TOKENS) + tokens

    def llm_remaining_seconds(self) -> float:
        """Вернуть минимум общего и LLM deadline в секундах.

        Timeout/terminal outcome даёт SecurityPolicyError; egress не разрешается."""
        return self.remaining_seconds(Resource.LLM_TIME_MS)

    def bounded_parser(self, parser: Parser) -> Parser:
        """Сузить limits поддержанного builtin, не меняя исходный adapter."""
        from .parsers import bounded_parser

        self._active()
        return bounded_parser(self, parser)

    def cancel(self) -> None:
        """Закрыть run и сохранить safe cancellation evidence без raw exception."""
        with self._lock:
            self._record("SECURITY_OPERATION_CANCELLED", Resource.PARSER_TIME_MS)

    def guarded_reader(
        self, reader: SourceReader, source: SourceArtifact
    ) -> SourceReader:
        """Вернуть bounded reader, связанный с размером и fingerprint source.

        reader — caller-owned SourceReader, source — точный SourceArtifact.
        При чтении проверяются bounds/identity; drift и over-return закрывают run.
        SecurityPolicyError скрывает чужие diagnostics. Lease не проверяет filesystem
        roots/symlinks и не закрывает transport; это обязанности host."""
        from .source import GuardedReader

        return GuardedReader(reader, source, self)

    def llm_policy(self, local: LLMRoutingPolicy) -> LLMRoutingPolicy:
        """Сузить budget до approval; router также получает resources=self."""
        from .compilation import llm_policy

        self._active()
        return llm_policy(self.policy.limits, local)

    def inspection_limits(self, local: InspectionLimits) -> InspectionLimits:
        """Сузить limits до создания target/fingerprint; adapter получает resources=self."""
        from .compilation import inspection_limits

        self._active()
        return inspection_limits(self.policy.limits, local)

    def load_policy(self, local: PostgreSQLLoadPolicy) -> PostgreSQLLoadPolicy:
        """Сузить существующие read/write batch caps до staging/load planning."""
        from .compilation import load_policy

        self._active()
        return load_policy(self.policy.limits, local)

    @asynccontextmanager
    async def parse(
        self,
        parser: Parser,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[AsyncIterator[ExtractedBatch]]:
        """Probe и parse через existing registry с конечным общим deadline.

        Контекст обязателен для cleanup при раннем выходе. Таймер окружает только
        parser awaits и не отменяет consumer между batches. Один parse на session;
        replay требует нового run. Неподдержанный parser отказывает до чтения.
        """
        from .parsers import bounded_parser
        from .source import GuardedReader

        if (
            self._parser_used
            or type(source) is not SourceArtifact
            or type(context) is not ParseContext
        ):
            self.deny("SECURITY_INPUT_REJECTED")
        bounded = bounded_parser(self, parser, context=context)
        self._parser_used = True
        self.check(Resource.FILE_BYTES, source.size_bytes)
        self.check(Resource.STREAM_BYTES, source.size_bytes)
        limits = self.policy.limits
        reader = GuardedReader(context.reader, source, self)
        constrained = replace(
            context,
            reader=reader,
            max_bytes=min(
                context.max_bytes, limits.max_file_bytes, limits.max_stream_bytes
            ),
            max_records=min(context.max_records, limits.max_records),
            max_nesting_depth=min(context.max_nesting_depth, limits.max_nesting_depth),
            max_text_chars=min(context.max_text_chars, limits.max_text_chars),
            max_columns=min(context.max_columns, limits.max_columns),
            batch_options=replace(
                context.batch_options,
                max_batches=min(context.batch_options.max_batches, limits.max_chunks),
            ),
        )
        registry = ParserRegistry()
        registry.register(bounded)
        try:
            async with registry.session() as lease:
                selected = await self.call(
                    lambda: lease.select(
                        source,
                        ProbeContext(
                            reader=reader,
                            source_fingerprint=context.source_fingerprint,
                            max_probe_bytes=min(
                                source.size_bytes or 1,
                                65_536,
                                limits.max_text_chars * 4,
                            ),
                        ),
                    ),
                    resource=Resource.PARSER_TIME_MS,
                )
                if selected.probe_result.format_id not in self.policy.allowed_formats:
                    self.deny("SECURITY_INPUT_REJECTED")
                stream = selected.parse(source, constrained)

                async def iterate() -> AsyncIterator[ExtractedBatch]:
                    while True:
                        try:
                            batch = await self.call(
                                lambda: anext(stream), resource=Resource.PARSER_TIME_MS
                            )
                        except StopAsyncIteration:
                            return
                        yield batch

                yield iterate()
        except asyncio.CancelledError:
            with self._lock:
                self._record("SECURITY_OPERATION_CANCELLED", Resource.PARSER_TIME_MS)
            raise asyncio.CancelledError from None

    async def snapshot(
        self,
        reader: SourceStream,
        *,
        kind: str,
        expected_size: int | None = None,
    ) -> BoundedSnapshot:
        """Прочитать один источник в ограниченный immutable snapshot в памяти.

        Args:
            reader: Transport с async read(size), выдающим максимум size bytes.
            kind: Только file или stream; выбирает cap, но не открывает путь.
            expected_size: Необязательный точный размер; проверяется до I/O и на EOF.

        Returns:
            BoundedSnapshot с size_bytes, source_fingerprint и offset-based read.

        Raises:
            SecurityPolicyError: Неверный input/размер, повтор snapshot, cap/deadline
                или SECURITY_OPERATION_FAILED без foreign diagnostics.
            asyncio.CancelledError: Отмена без raw текста; session закрывается.

        Reader остаётся caller-owned. Bytes проверяются до накопления; финальная
        immutable копия увеличивает transient memory. Payload cap не ограничивает RSS."""
        from .source import snapshot

        with self._lock:
            self._active()
            if self._snapshot_used:
                self.deny("SECURITY_INPUT_REJECTED")
            self._snapshot_used = True
        return await snapshot(self, reader, kind=kind, expected_size=expected_size)
