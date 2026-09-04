"""Runtime boundary проверенного потока ``ExtractedBatch``."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Never, Protocol, cast, runtime_checkable

from structuraguard.contracts.common import PhysicalSourceRef
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedBatchSummary,
    ProbeResult,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.ports.source import ParseContext

from ._registration import RegisteredParser


@runtime_checkable
class _AsyncClosable(Protocol):
    def aclose(self) -> Awaitable[object]: ...


type _TrackStream = Callable[[ValidatedParserStream], None]
type _StreamFactory = Callable[[], ValidatedParserStream]
type _CreateStream = Callable[[_StreamFactory], ValidatedParserStream]
type _PrimaryStreamError = ParserError | SecurityPolicyError
type _CleanupStreamError = ParserError | asyncio.CancelledError
type _BoundaryError = ParserError | SecurityPolicyError | asyncio.CancelledError
type _BoundaryFailureFactory = Callable[
    [BaseException], ParserError | SecurityPolicyError
]


def _output_error(
    adapter_id: str,
    reason: str,
    *,
    cause: BaseException | None = None,
    retryable: bool = False,
) -> ParserError:
    return ParserError(
        error_code="PARSER_OUTPUT_INVALID",
        message="Parser нарушил contract физического ExtractedBatch stream.",
        details={"adapter_id": adapter_id, "reason": reason},
        cause=cause,
        retryable=retryable,
    )


def _detach_security_error(error: SecurityPolicyError) -> SecurityPolicyError:
    """Удалить чужую exception chain перед сохранением typed security outcome."""

    error.__context__ = None
    error.__cause__ = None
    error.__traceback__ = None
    if hasattr(error, "__notes__"):
        del error.__notes__
    return error


def _sanitize_cancelled_error(
    error: asyncio.CancelledError,
) -> asyncio.CancelledError:
    """Заменить недоверенное cancellation безопасным control-flow outcome."""

    del error
    sanitized = asyncio.CancelledError()
    sanitized.__suppress_context__ = True
    return sanitized


def _raise_sanitized_cancelled(error: asyncio.CancelledError) -> Never:
    """Поднять exact cancellation без активной недоверенной exception chain."""

    sanitized = _sanitize_cancelled_error(error)
    try:
        raise sanitized from None
    except asyncio.CancelledError as outcome:
        outcome.__context__ = None
        outcome.__cause__ = None
        outcome.__traceback__ = None
        raise


def _raise_detached_group(
    message: str,
    outcomes: tuple[BaseException, ...],
) -> Never:
    """Поднять aggregate outcome без неявной недоверенной exception chain."""

    try:
        raise BaseExceptionGroup(message, outcomes) from None
    except BaseExceptionGroup as group:
        group.__context__ = None
        group.__cause__ = None
        group.__traceback__ = None
        if hasattr(group, "__notes__"):
            group.__notes__ = []
        raise


def _normalize_boundary_group(
    error: BaseExceptionGroup[BaseException],
    failure_factory: _BoundaryFailureFactory,
) -> tuple[_BoundaryError, ...]:
    """Нормализовать leaves недоверенной exception group на public boundary."""

    outcomes: list[_BoundaryError] = []
    for leaf in _flatten_exception_group(error):
        if isinstance(leaf, asyncio.CancelledError):
            outcomes.append(_sanitize_cancelled_error(leaf))
        else:
            outcomes.append(failure_factory(leaf))
    return tuple(outcomes)


def _raise_boundary_outcomes(
    message: str,
    outcomes: tuple[_BoundaryError, ...],
) -> Never:
    """Поднять один либо несколько уже санитизированных boundary outcomes."""

    if len(outcomes) == 1:
        outcome = outcomes[0]
        if isinstance(outcome, asyncio.CancelledError):
            _raise_sanitized_cancelled(outcome)
        raise outcome from None
    _raise_detached_group(message, outcomes)


def _iterator_close_error(
    adapter_id: str,
    cause: BaseException,
) -> ParserError:
    return _output_error(
        adapter_id,
        "iterator_close_failed",
        cause=cause,
        retryable=True,
    )


def _flatten_exception_group(
    error: BaseExceptionGroup[BaseException],
) -> tuple[BaseException, ...]:
    leaves: list[BaseException] = []
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            nested = BaseExceptionGroup.__getattribute__(current, "exceptions")
            pending.extend(reversed(nested))
            continue
        leaves.append(current)
    return tuple(leaves)


def _detach_cleanup_error(error: ParserError) -> ParserError:
    error.__context__ = None
    error.__cause__ = None
    error.__traceback__ = None
    if hasattr(error, "__notes__"):
        error.__notes__ = []
    return error


class SelectedParser:
    """Session-bound handle выбранного trusted parser.

    Handle возвращается ``ParserRegistrySession.select`` и действует только пока
    открыта породившая его session. Он хранит проверенные identity, полный source
    snapshot и probe result; вручную конструировать его из внутренних registration
    callbacks не следует. Manual parser выполняется in-process и поэтому должен
    быть доверенным.
    """

    __slots__ = (
        "_create_stream",
        "_probe_result",
        "_registration",
        "_release_stream",
        "_source",
    )

    def __init__(
        self,
        *,
        registration: RegisteredParser,
        probe_result: ProbeResult,
        source: SourceArtifact,
        create_stream: _CreateStream,
        release_stream: _TrackStream,
    ) -> None:
        self._registration = registration
        self._probe_result = probe_result
        self._source = source
        self._create_stream = create_stream
        self._release_stream = release_stream

    @property
    def adapter_id(self) -> str:
        """Вернуть проверенный идентификатор выбранного parser adapter."""

        return self._registration.identity.adapter_id

    @property
    def version(self) -> str:
        """Вернуть версию adapter, зафиксированную при регистрации."""

        return self._registration.identity.version

    @property
    def priority(self) -> int:
        """Вернуть приоритет adapter, использованный при selection."""

        return self._registration.identity.priority

    @property
    def probe_result(self) -> ProbeResult:
        """Вернуть immutable probe snapshot, использованный при selection."""

        return self._probe_result

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> ValidatedParserStream:
        """Начать technical parsing выбранным parser.

        Args:
            source: Тот же полный immutable snapshot, для которого выполнен probe.
            context: Reader и конечные resource limits parsing.

        Returns:
            Session-owned поток проверяемых физических ``ExtractedBatch``.

        Raises:
            asyncio.CancelledError: Если trusted parser отменил синхронный
                вызов; payload и exception chain cancellation удаляются.
            BaseExceptionGroup: Если parser вернул несколько control-flow и
                обычных failures; leaves и chain санитизированы.
            ParserError: Если session закрыта, source/context не совпадают,
                parser не вернул async iterator или его вызов завершился ошибкой.
            SecurityPolicyError: Если trusted parser отклонил операцию policy.

        Вызов исполняет manual parser in-process. Stream проверяет exact DTO,
        identity, lineage, последовательность, terminal manifest и ``max_records``.
        Parser не получает LLM или DB ports и не возвращает ``MappingPlan`` либо
        готовые бизнес-сущности.
        """

        def factory() -> ValidatedParserStream:
            if source != self._source:
                raise _output_error(self.adapter_id, "selected_source_identity")
            if context.source_fingerprint != source.source_fingerprint:
                raise _output_error(self.adapter_id, "context_source_identity")
            cancellation: asyncio.CancelledError | None = None
            grouped_failure: tuple[_BoundaryError, ...] = ()
            call_failure: ParserError | SecurityPolicyError | None = None
            try:
                returned: object = self._registration.parser.parse(source, context)
            except BaseExceptionGroup as error:
                if isinstance(error, ExceptionGroup):
                    call_failure = _output_error(
                        self.adapter_id,
                        "parse_call_failed",
                        cause=error,
                    )
                else:
                    grouped_failure = _normalize_boundary_group(
                        error,
                        self._parse_call_group_failure,
                    )
            except asyncio.CancelledError as error:
                cancellation = _sanitize_cancelled_error(error)
            except SecurityPolicyError as error:
                call_failure = _detach_security_error(error)
            except Exception as error:
                call_failure = _output_error(
                    self.adapter_id,
                    "parse_call_failed",
                    cause=error,
                )
            if cancellation is not None:
                raise cancellation from None
            if grouped_failure:
                _raise_boundary_outcomes(
                    "Parser call завершился с несколькими ошибками.",
                    grouped_failure,
                )
            if call_failure is not None:
                raise call_failure from None
            if not isinstance(returned, AsyncIterator):
                if inspect.iscoroutine(returned):
                    returned.close()
                raise _output_error(self.adapter_id, "parse_not_async_iterator")
            # Generic item type нельзя доказать runtime-проверкой; каждый item
            # валидируется exact-type до передачи consumer.
            iterator = cast(AsyncIterator[ExtractedBatch], returned)
            return ValidatedParserStream(
                iterator=iterator,
                source=source,
                registration=self._registration,
                max_records=context.max_records,
                release=self._release_stream,
            )

        return self._create_stream(factory)

    def _parse_call_group_failure(
        self,
        error: BaseException,
    ) -> ParserError | SecurityPolicyError:
        if isinstance(error, SecurityPolicyError):
            return _detach_security_error(error)
        return _output_error(
            self.adapter_id,
            "parse_call_failed",
            cause=error,
        )


class ValidatedParserStream(AsyncIterator[ExtractedBatch]):
    """Session-owned поток физического результата parser.

    При последовательном чтении поток повторно валидирует exact ``ExtractedBatch``,
    source/parser identity, extraction identity, индексы, fingerprints, terminal
    manifest и предел ``max_records``. Одновременные вызовы ``anext`` запрещены.
    Полное нормальное исчерпание закрывает underlying iterator; неуспешный
    automatic cleanup quarantines поток до явного retry через ``aclose``.
    Ранний выход требует ``aclose`` или выхода из ``ParserRegistrySession``.
    """

    __slots__ = (
        "_closed",
        "_closing",
        "_completed",
        "_extraction_id",
        "_fingerprints",
        "_iterator",
        "_max_records",
        "_next_in_progress",
        "_next_index",
        "_operation_lock",
        "_physical_refs",
        "_registration",
        "_release",
        "_source",
        "_summaries",
        "_terminal_seen",
        "_total_records",
        "_tracking_released",
    )

    def __init__(
        self,
        *,
        iterator: AsyncIterator[ExtractedBatch],
        source: SourceArtifact,
        registration: RegisteredParser,
        max_records: int,
        release: _TrackStream,
    ) -> None:
        self._iterator = iterator
        self._source = source
        self._registration = registration
        self._max_records = max_records
        self._release = release
        self._operation_lock = asyncio.Lock()
        self._next_in_progress = False
        self._next_index = 0
        self._extraction_id: str | None = None
        self._fingerprints: set[str] = set()
        self._physical_refs: set[PhysicalSourceRef] = set()
        self._summaries: list[ExtractedBatchSummary] = []
        self._total_records = 0
        self._terminal_seen = False
        self._tracking_released = False
        self._closing = False
        self._closed = False
        self._completed = False

    @property
    def completed(self) -> bool:
        """Вернуть ``True`` только после normal exhaustion и успешного cleanup.

        Раннее закрытие и любой ошибочный исход оставляют значение ``False``.
        """

        return self._completed

    def __aiter__(self) -> ValidatedParserStream:
        """Вернуть этот single-consumer async iterator."""

        return self

    def _begin_close(self) -> None:
        """Запретить новый output до асинхронного закрытия iterator."""

        self._closing = True

    async def __anext__(self) -> ExtractedBatch:
        """Вернуть следующий проверенный физический batch.

        Raises:
            StopAsyncIteration: После terminal batch и успешного cleanup либо
                после начала закрытия потока.
            asyncio.CancelledError: С сохранением control-flow cancellation,
                но без недоверенных payload, chain и notes; поток немедленно
                quarantined и остаётся owned session до cleanup.
            BaseExceptionGroup: Primary contract/policy error вместе с ошибкой
                cleanup либо grouped failure parser; outcomes сохранены в
                безопасном виде, поток остаётся quarantined.
            ParserError: При нарушении output contract или concurrent iteration.
            SecurityPolicyError: При превышении ``max_records`` или policy error
                underlying parser.
        """

        if self._closing or self._closed:
            raise StopAsyncIteration
        if self._next_in_progress:
            raise _output_error(
                self._registration.identity.adapter_id,
                "concurrent_iteration",
            )
        self._next_in_progress = True
        try:
            async with self._operation_lock:
                if self._closing or self._closed:
                    raise StopAsyncIteration
                return await self._next_locked()
        finally:
            self._next_in_progress = False

    async def _next_locked(self) -> ExtractedBatch:
        cancellation: asyncio.CancelledError | None = None
        exhausted = False
        grouped_failure: tuple[_BoundaryError, ...] = ()
        security_failure: SecurityPolicyError | None = None
        iteration_failure: ParserError | None = None
        try:
            item = await anext(self._iterator)
        except BaseExceptionGroup as error:
            if isinstance(error, ExceptionGroup):
                iteration_failure = _output_error(
                    self._registration.identity.adapter_id,
                    "parse_iteration_failed",
                    cause=error,
                )
            else:
                self._closing = True
                grouped_failure = _normalize_boundary_group(
                    error,
                    self._iteration_group_failure,
                )
        except asyncio.CancelledError as error:
            # Отменённый adapter read мог уже сдвинуть внутренний cursor.
            # Quarantine запрещает продолжение потенциально искажённой sequence;
            # session сохраняет ownership и закроет iterator при выходе.
            self._closing = True
            cancellation = _sanitize_cancelled_error(error)
        except StopAsyncIteration:
            exhausted = True
        except SecurityPolicyError as error:
            security_failure = _detach_security_error(error)
        except Exception as error:
            iteration_failure = _output_error(
                self._registration.identity.adapter_id,
                "parse_iteration_failed",
                cause=error,
            )

        if cancellation is not None:
            raise cancellation from None
        if grouped_failure:
            await self._raise_group_after_cleanup(grouped_failure)
        if exhausted:
            if not self._terminal_seen:
                await self._fail_locked("missing_terminal_manifest")
            # Quarantine устанавливается до potentially failing cleanup: после
            # ошибки продолжить output нельзя, retry разрешён только через aclose.
            self._closing = True
            await self._close_underlying_locked()
            self._completed = True
            raise StopAsyncIteration
        if security_failure is not None:
            await self._raise_after_cleanup(security_failure)
        if iteration_failure is not None:
            await self._fail_error_locked(iteration_failure)

        if self._closing:
            await self._close_underlying_locked()
            raise StopAsyncIteration

        if self._terminal_seen:
            await self._fail_locked("batch_after_terminal")
        if type(item) is not ExtractedBatch:
            await self._fail_locked("batch_type")
        preflight_failure: ParserError | None = None
        try:
            batch_record_count = self._preflight_record_count(item)
        except Exception as error:  # parser DTO является недоверенной boundary
            preflight_failure = _output_error(
                self._registration.identity.adapter_id,
                "batch_schema",
                cause=error,
            )
        if preflight_failure is not None:
            await self._fail_error_locked(preflight_failure)
        if batch_record_count is None:
            await self._fail_limit_locked()
        validation_failure: ParserError | None = None
        try:
            item = self._revalidate_batch(item)
            self._validate_batch(item)
        except ParserError as error:
            validation_failure = error
        if validation_failure is not None:
            await self._raise_after_cleanup(validation_failure)

        summary = item.to_summary()
        self._physical_refs.update(item.physical_refs())
        self._summaries.append(summary)
        self._next_index += 1
        self._fingerprints.add(item.batch_fingerprint)
        self._total_records += batch_record_count

        if item.is_last:
            manifest = item.manifest
            if manifest is None or manifest.batches != tuple(self._summaries):
                await self._fail_locked("manifest_sequence")
            if not set(manifest.source_index.refs) <= self._physical_refs:
                await self._fail_locked("manifest_source_index")
            self._terminal_seen = True
        return item

    def _iteration_group_failure(
        self,
        error: BaseException,
    ) -> ParserError | SecurityPolicyError:
        if isinstance(error, SecurityPolicyError):
            return _detach_security_error(error)
        return _output_error(
            self._registration.identity.adapter_id,
            "parse_iteration_failed",
            cause=error,
        )

    async def aclose(self) -> None:
        """Идемпотентно закрыть underlying iterator и освободить session tracking.

        Raises:
            asyncio.CancelledError: Если cleanup отменён; payload и exception
                chain cancellation удаляются, повтор разрешён через ``aclose``.
            ParserError: Если underlying iterator не удалось закрыть корректно;
                ошибка имеет ``retryable=True``.
            BaseExceptionGroup: Если underlying cleanup вернул несколько
                outcomes; каждый leaf нормализован до безопасной typed ошибки
                либо очищенного cancellation.
        """

        if self._closed:
            return
        self._closing = True
        try:
            async with self._operation_lock:
                await self._close_underlying_locked()
        except asyncio.CancelledError as error:
            _raise_sanitized_cancelled(error)

    async def _close_underlying_locked(self) -> None:
        if self._closed:
            return
        close_outcomes: tuple[_CleanupStreamError, ...] = ()
        try:
            if isinstance(self._iterator, _AsyncClosable):
                await self._iterator.aclose()
        except BaseExceptionGroup as error:
            close_outcomes = self._normalize_raw_close_failure(error)
        except asyncio.CancelledError as error:
            close_outcomes = (_sanitize_cancelled_error(error),)
        except Exception as error:
            close_outcomes = (
                _iterator_close_error(
                    self._registration.identity.adapter_id,
                    error,
                ),
            )
        else:
            self._closed = True
        finally:
            self._release_tracking()
        self._raise_cleanup_outcomes(close_outcomes)

    def _normalize_raw_close_failure(
        self,
        error: BaseExceptionGroup[BaseException],
    ) -> tuple[_CleanupStreamError, ...]:
        adapter_id = self._registration.identity.adapter_id
        outcomes: list[_CleanupStreamError] = []
        for leaf in _flatten_exception_group(error):
            if isinstance(leaf, asyncio.CancelledError):
                outcomes.append(_sanitize_cancelled_error(leaf))
            else:
                outcomes.append(_iterator_close_error(adapter_id, leaf))
        return tuple(outcomes)

    def _normalize_cleanup_failure(
        self,
        error: BaseException,
    ) -> tuple[_CleanupStreamError, ...]:
        """Нормализовать cleanup outcome перед session aggregation."""

        if isinstance(error, BaseExceptionGroup):
            outcomes: list[_CleanupStreamError] = []
            for leaf in _flatten_exception_group(error):
                outcomes.extend(self._normalize_cleanup_failure(leaf))
            return tuple(outcomes)
        if isinstance(error, asyncio.CancelledError):
            return (_sanitize_cancelled_error(error),)
        if type(error) is ParserError:
            return (_detach_cleanup_error(error),)
        return (
            _iterator_close_error(
                self._registration.identity.adapter_id,
                error,
            ),
        )

    @staticmethod
    def _raise_cleanup_outcomes(
        outcomes: tuple[_CleanupStreamError, ...],
    ) -> None:
        if not outcomes:
            return
        if len(outcomes) == 1:
            raise outcomes[0] from None
        raise BaseExceptionGroup(
            "Parser iterator cleanup завершился с несколькими ошибками.",
            outcomes,
        ) from None

    def _preflight_record_count(self, item: ExtractedBatch) -> int | None:
        """Посчитать physical records до дорогой deep-copy, остановившись на limit."""

        remaining = self._max_records - self._total_records
        count = 0

        def include(quantity: int) -> bool:
            nonlocal count
            if quantity > remaining - count:
                return False
            count += quantity
            return True

        if not include(len(item.lines)):
            return None
        if not include(len(item.blocks)):
            return None
        if not include(len(item.tables)):
            return None
        if not include(len(item.trees)):
            return None
        for block in item.blocks:
            if not include(len(block.lines)):
                return None
        for table in item.tables:
            if not include(len(table.cells)):
                return None

        for line in item.lines:
            for value in line.values:
                if value.value_id is not None and not include(1):
                    return None
        for block in item.blocks:
            for value in block.values:
                if value.value_id is not None and not include(1):
                    return None
            for line in block.lines:
                for value in line.values:
                    if value.value_id is not None and not include(1):
                        return None
        for table in item.tables:
            for cell in table.cells:
                if cell.value.value_id is not None and not include(1):
                    return None
        for node in item.trees:
            if (
                node.value is not None
                and node.value.value_id is not None
                and not include(1)
            ):
                return None

        if count == 0 and not include(1):
            return None
        return count

    def _release_tracking(self) -> None:
        if self._tracking_released:
            return
        self._tracking_released = True
        self._release(self)

    def _validate_batch(self, item: ExtractedBatch) -> None:
        identity = self._registration.identity
        if item.source != self._source.ref:
            raise _output_error(identity.adapter_id, "source_identity")
        if (
            item.parser_id != identity.adapter_id
            or item.parser_version != identity.version
        ):
            raise _output_error(identity.adapter_id, "parser_identity")
        if item.batch_index != self._next_index:
            raise _output_error(identity.adapter_id, "batch_sequence")
        if item.batch_fingerprint in self._fingerprints:
            raise _output_error(identity.adapter_id, "batch_fingerprint_duplicate")
        if self._extraction_id is None:
            self._extraction_id = item.extraction_id
        elif item.extraction_id != self._extraction_id:
            raise _output_error(identity.adapter_id, "extraction_identity")

    def _revalidate_batch(self, item: ExtractedBatch) -> ExtractedBatch:
        validation_failure: ParserError | None = None
        try:
            payload = item.model_dump(
                mode="python",
                round_trip=True,
                warnings="error",
            )
            validated = ExtractedBatch.model_validate(payload, strict=True)
        except Exception as error:  # parser DTO является недоверенной boundary
            validation_failure = _output_error(
                self._registration.identity.adapter_id,
                "batch_schema",
                cause=error,
            )
        if validation_failure is not None:
            raise validation_failure from None
        return validated

    async def _fail_locked(
        self,
        reason: str,
    ) -> Never:
        await self._fail_error_locked(
            _output_error(
                self._registration.identity.adapter_id,
                reason,
            )
        )

    async def _fail_error_locked(self, error: ParserError) -> Never:
        await self._raise_after_cleanup(error)

    async def _fail_limit_locked(self) -> Never:
        await self._raise_after_cleanup(
            SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED",
                message="Parser output превысил max_records текущего ParseContext.",
                details={
                    "adapter_id": self._registration.identity.adapter_id,
                    "limit": self._max_records,
                    "resource": "records",
                },
            )
        )

    async def _raise_after_cleanup(self, primary: _PrimaryStreamError) -> Never:
        self._closing = True
        cleanup_outcomes: tuple[_CleanupStreamError, ...] = ()
        try:
            await self._close_underlying_locked()
        except BaseExceptionGroup as error:
            cleanup_outcomes = self._normalize_cleanup_failure(error)
        except (ParserError, asyncio.CancelledError) as error:
            cleanup_outcomes = self._normalize_cleanup_failure(error)
        if cleanup_outcomes:
            raise BaseExceptionGroup(
                "Parser stream завершился с primary и cleanup errors.",
                (primary, *cleanup_outcomes),
            ) from None
        raise primary from None

    async def _raise_group_after_cleanup(
        self,
        primary_outcomes: tuple[_BoundaryError, ...],
    ) -> Never:
        self._closing = True
        cleanup_outcomes: tuple[_CleanupStreamError, ...] = ()
        try:
            await self._close_underlying_locked()
        except BaseExceptionGroup as error:
            cleanup_outcomes = self._normalize_cleanup_failure(error)
        except (ParserError, asyncio.CancelledError) as error:
            cleanup_outcomes = self._normalize_cleanup_failure(error)
        _raise_boundary_outcomes(
            "Parser stream завершился с несколькими ошибками.",
            (*primary_outcomes, *cleanup_outcomes),
        )
