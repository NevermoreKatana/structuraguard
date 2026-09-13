"""Runner composition: existing parser validation + trusted OS sandbox port."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from uuid import UUID

from pydantic import ValidationError

from structuraguard.contracts.sandbox import (
    SandboxCapabilities,
    SandboxExit,
    SandboxParserSpec,
    SandboxPolicy,
    SandboxRequest,
)
from structuraguard.contracts.security import Resource
from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.exceptions import SecurityPolicyError, StructuraGuardError
from structuraguard.parsers.registry import ParserRegistry
from structuraguard.ports.parser import Parser
from structuraguard.ports.resources import ParserResourceGuard
from structuraguard.ports.sandbox import SandboxBackend, SandboxProcess
from structuraguard.ports.source import ParseContext, ProbeContext


def _failure(code: str) -> SecurityPolicyError:
    return SecurityPolicyError(
        error_code=code, message="Parser runner отклонил операцию."
    )


def _consume[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        task.exception()


async def _bounded_await[T](operation: Callable[[], Awaitable[T]], seconds: float) -> T:
    """Ожидание host adapter ограничено даже при подавленной им cancellation."""
    code, cancelled = "SECURITY_SANDBOX_FAILED", False
    task: asyncio.Future[T] | None = None
    try:
        task = asyncio.ensure_future(operation())
        done, _ = await asyncio.wait((task,), timeout=seconds)
        if not done:
            code = "PROCESSING_TIMEOUT"
        else:
            return task.result()
    except asyncio.CancelledError:
        cancelled = True
    except StopAsyncIteration:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        if isinstance(error, StructuraGuardError) and error.error_code in {
            "SECURITY_LIMIT_EXCEEDED",
            "SECURITY_SANDBOX_REQUIRED",
            "PARSER_OUTPUT_INVALID",
            "PROCESSING_TIMEOUT",
            "SECURITY_SANDBOX_CLEANUP_FAILED",
        }:
            code = error.error_code
    finally:
        if task is not None and not task.done():
            task.cancel()
            task.add_done_callback(_consume)
    if cancelled:
        raise asyncio.CancelledError
    raise _failure(code)


async def _settle_start(
    task: asyncio.Task[SandboxProcess], seconds: float
) -> tuple[SandboxProcess | None, bool]:
    """Забрать handle даже при timeout/cancel между возвратом start и admission."""
    cancelled = False
    deadline = asyncio.get_running_loop().time() + seconds
    if not task.done():
        task.cancel()
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            task.add_done_callback(_consume)
            raise _failure("SECURITY_SANDBOX_CLEANUP_FAILED")
        try:
            await asyncio.wait((task,), timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    if not task.cancelled() and task.exception() is None:
        return task.result(), cancelled
    return None, cancelled


class InProcessParserRunner:
    """Выполнять разрешённый builtin внутри host process с общим resource guard.

    parser — точный поддержанный builtin, session — ParserResourceGuard одного run.
    Конструктор не выполняет I/O; admission проверяется при parse. Исходные данные
    остаются недоверенными. OS CPU/RSS/network isolation здесь не обеспечивается;
    default sandbox_required и strict risky formats запрещают in-process parse."""

    def __init__(self, parser: Parser, *, session: ParserResourceGuard) -> None:
        self._parser = parser
        self._session = session

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AbstractAsyncContextManager[AsyncIterator[ExtractedBatch]]:
        """Вернуть async context manager verified batches для source/context.

        Делегирует session.parse с registry validation и cooperative deadline;
        ошибки policy/limits дают SecurityPolicyError, отмена — CancelledError.
        Context manager обязателен для cleanup при раннем выходе; EOF завершает extraction."""
        return self._session.parse(self._parser, source, context)


class _SandboxAdapter:
    """Host proxy импортирует только DTO; plugin object здесь не создаётся."""

    def __init__(self, runner: SandboxParserRunner, process: SandboxProcess) -> None:
        self._runner, self._process = runner, process
        self.adapter_id = runner._spec.descriptor.adapter_id
        self.version = runner._spec.parser_version
        self.priority = 0

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        raw = await self._runner._call(
            lambda: self._process.probe(max_bytes=self._runner._frame_limit())
        )
        raw = self._runner._frame(raw)
        result = ProbeResult.model_validate_json(raw)
        if result.format_id != self._runner._spec.format_id:
            raise _failure("PARSER_OUTPUT_INVALID")
        return result

    async def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        while True:
            raw = await self._runner._call(
                lambda: self._process.read_frame(max_bytes=self._runner._frame_limit())
            )
            if raw is None:
                outcome = await self._runner._call(self._process.wait)
                self._runner._exit(outcome, completed=True)
                return
            raw = self._runner._frame(raw)
            yield ExtractedBatch.model_validate_json(raw)


class SandboxParserRunner:
    """Проверять admission/IPC и registry contract внешнего sandbox backend.

    Args:
        spec: Immutable pinned artifact/entry point/version/format.
        policy: Explicit spec allowlist и конечные result/frame/cleanup caps.
        session: Общий guard, закрывающий run после failure/cancel.
        backend: Trusted OS adapter; None не разрешает in-process fallback.
        run_id: Тот же UUID, что у session.

    Конструктор ревалидирует DTO без import/I/O: неверные DTO дают ValidationError,
    run binding — SecurityPolicyError. Один instance обслуживает один parse.
    Core поставляет orchestration, а не container runtime; backend обеспечивает
    OS/network/temp isolation и bounded transport до накопления."""

    def __init__(
        self,
        spec: SandboxParserSpec,
        *,
        policy: SandboxPolicy,
        session: ParserResourceGuard,
        backend: SandboxBackend | None,
        run_id: UUID,
    ) -> None:
        self._spec = SandboxParserSpec.model_validate(spec.model_dump(warnings="error"))
        self._policy = SandboxPolicy.model_validate(policy.model_dump(warnings="error"))
        self._session, self._backend, self._run = session, backend, run_id
        if type(run_id) is not UUID or run_id != session.run_id:
            session.deny("SECURITY_POLICY_INVALID")
        self._used, self._bytes = False, 0

    def _frame_limit(self) -> int:
        # +1 позволяет увидеть точное превышение, без чтения arbitrary frame.
        return (
            min(
                self._policy.max_frame_bytes,
                self._policy.max_result_bytes - self._bytes,
            )
            + 1
        )

    def _frame(self, raw: bytes) -> bytes:
        if type(raw) is not bytes:
            raise _failure("PARSER_OUTPUT_INVALID")
        if (
            len(raw) > self._policy.max_frame_bytes
            or len(raw) > self._policy.max_result_bytes - self._bytes
        ):
            self._session.deny("SECURITY_LIMIT_EXCEEDED")
        self._bytes += len(raw)
        depth, quoted, escaped = 0, False, False
        for char in raw:
            if quoted:
                if escaped:
                    escaped = False
                elif char == 92:
                    escaped = True
                elif char == 34:
                    quoted = False
            elif char == 34:
                quoted = True
            elif char in (91, 123):
                depth += 1
                if depth > self._policy.max_json_depth:
                    self._session.deny("SECURITY_LIMIT_EXCEEDED")
            elif char in (93, 125):
                depth -= 1
        return raw

    async def _call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        try:
            seconds = self._session.remaining_seconds(Resource.PARSER_TIME_MS)
            result = await _bounded_await(operation, seconds)
            self._session.remaining_seconds(Resource.PARSER_TIME_MS)
            return result
        except SecurityPolicyError as error:
            self._session.record_failure(error.error_code)
            raise

    @staticmethod
    def _exit(value: SandboxExit, *, completed: bool = False) -> None:
        if type(value) is not SandboxExit:
            raise _failure("SECURITY_SANDBOX_CLEANUP_FAILED")
        accepted = False
        try:
            value = SandboxExit.model_validate(value.model_dump(warnings="error"))
            accepted = value.cleanup_complete and (
                not completed or value.status == "completed"
            )
        except (ValueError, TypeError):
            pass
        if not accepted:
            raise _failure("SECURITY_SANDBOX_CLEANUP_FAILED")

    def _admit_backend(self, backend: SandboxBackend) -> None:
        accepted = False
        cancelled = False
        try:
            caps = backend.capabilities
            accepted = (
                type(caps) is SandboxCapabilities
                and SandboxCapabilities.model_validate(
                    caps.model_dump(warnings="error")
                ).complete
            )
        except asyncio.CancelledError:
            cancelled = True
        except BaseException as error:
            # Getter стороннего host adapter может вернуть ошибку с credentials.
            if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
        if cancelled:
            self._session.cancel()
            raise asyncio.CancelledError
        if not accepted:
            self._session.deny("SECURITY_SANDBOX_REQUIRED")

    @asynccontextmanager
    async def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[AsyncIterator[ExtractedBatch]]:
        """Выдать verified batches из source/context через async context manager.

        До source read проверяет spec/format/capabilities; отказ — SECURITY_SANDBOX_REQUIRED.
        Неверный output — PARSER_OUTPUT_INVALID; лимиты/deadline и отсутствие cleanup
        acknowledgement дают SecurityPolicyError. Ошибка/отмена закрывает общий run.
        Exit/early break всегда пытается завершить worker в отдельном cleanup budget.
        Backend отвечает за kill/reap также при отмене start; SDK не аттестует OS isolation."""
        backend = self._backend
        if (
            self._used
            or type(source) is not SourceArtifact
            or type(context) is not ParseContext
        ):
            self._session.deny("SECURITY_INPUT_REJECTED")
        if (
            backend is None
            or self._spec.fingerprint not in self._policy.allowed_specs
            or self._spec.format_id not in self._session.policy.allowed_formats
        ):
            self._session.deny("SECURITY_SANDBOX_REQUIRED")
        self._admit_backend(backend)
        self._used = True
        limits = self._session.limits
        self._session.check(Resource.FILE_BYTES, source.size_bytes)
        self._session.check(Resource.STREAM_BYTES, source.size_bytes)
        reader = self._session.guarded_reader(context.reader, source)
        constrained = replace(
            context,
            reader=reader,
            max_bytes=min(
                context.max_bytes, limits.max_file_bytes, limits.max_stream_bytes
            ),
            max_records=min(context.max_records, limits.max_records),
            max_nesting_depth=min(context.max_nesting_depth, limits.max_nesting_depth),
            max_columns=min(context.max_columns, limits.max_columns),
            max_text_chars=min(context.max_text_chars, limits.max_text_chars),
            batch_options=replace(
                context.batch_options,
                max_batches=min(context.batch_options.max_batches, limits.max_chunks),
            ),
        )
        request = SandboxRequest(
            run_id=self._run,
            parser=self._spec,
            source=source.ref,
            source_size_bytes=source.size_bytes,
            limits=limits.model_copy(
                update={
                    "max_file_bytes": constrained.max_bytes,
                    "max_stream_bytes": constrained.max_bytes,
                    "max_records": constrained.max_records,
                    "max_nesting_depth": constrained.max_nesting_depth,
                    "max_columns": constrained.max_columns,
                    "max_text_chars": constrained.max_text_chars,
                    "max_chunks": constrained.batch_options.max_batches,
                }
            ),
            output_policy=self._policy,
        )
        process: SandboxProcess | None = None
        start_task: asyncio.Task[SandboxProcess] | None = None
        if source.size_bytes > constrained.max_bytes:
            self._session.deny(
                "SECURITY_LIMIT_EXCEEDED",
                Resource.FILE_BYTES,
                limit=constrained.max_bytes,
                observed=source.size_bytes,
            )
        try:
            self._session.remaining_seconds(Resource.PARSER_TIME_MS)
            start_task = asyncio.create_task(backend.start(request, reader))
            process = await self._call(lambda: asyncio.shield(start_task))
            registry = ParserRegistry()
            registry.register(_SandboxAdapter(self, process))
            async with registry.session() as lease:
                selected = await self._call(
                    lambda: lease.select(
                        source,
                        ProbeContext(
                            reader=reader,
                            source_fingerprint=context.source_fingerprint,
                            max_probe_bytes=min(source.size_bytes or 1, 65536),
                        ),
                    )
                )
                stream = selected.parse(source, constrained)

                async def iterate() -> AsyncIterator[ExtractedBatch]:
                    while True:
                        try:
                            batch = await self._call(lambda: anext(stream))
                        except StopAsyncIteration:
                            return
                        yield batch

                yield iterate()
        except asyncio.CancelledError:
            self._session.cancel()
            raise asyncio.CancelledError from None
        except (ValidationError, TypeError, ValueError):
            self._session.deny("SECURITY_INPUT_REJECTED")
        finally:
            try:
                cleanup_cancelled = False
                if start_task is not None:
                    process, cleanup_cancelled = await _settle_start(
                        start_task, self._policy.cleanup_time_ms / 1000
                    )
                if process is not None:
                    outcome = await _bounded_await(
                        process.aclose, self._policy.cleanup_time_ms / 1000
                    )
                    self._exit(outcome)
                if cleanup_cancelled:
                    raise asyncio.CancelledError
            except asyncio.CancelledError:
                self._session.cancel()
                raise asyncio.CancelledError from None
            except SecurityPolicyError as error:
                # Cleanup failure также запрещает downstream LLM/DB в этом run.
                self._session.record_failure(error.error_code)
                raise
