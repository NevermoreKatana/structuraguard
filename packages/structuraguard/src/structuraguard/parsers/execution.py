"""Runtime boundary проверенного потока ``ExtractedBatch``."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from enum import StrEnum
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

from ._hashing import batch_fingerprint, manifest_fingerprint
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
type _AllowedDetailPolicy = tuple[tuple[str, frozenset[str]], ...]

_MAX_INDEXED_REFS = 10_000
_MAX_SAFE_SOURCE_POSITION = 2**63 - 1


class _PreflightLimit(StrEnum):
    RECORDS = "records"
    PHYSICAL_OBJECTS = "physical_objects"


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


def _allowed_parser_error_policy(
    error_code: str,
) -> tuple[str, _AllowedDetailPolicy] | None:
    if error_code == "PARSER_MALFORMED_INPUT":
        return (
            "Parser отклонил некорректный входной формат.",
            (
                (
                    "reason",
                    frozenset(
                        {
                            "binary_content",
                            "dangling_escape",
                            "inconsistent_record",
                            "invalid_escape",
                            "invalid_json",
                            "invalid_xml",
                            "invalid_html",
                            "invalid_yaml",
                            "invalid_yaml_alias",
                            "invalid_yaml_anchor",
                            "invalid_unicode_scalar",
                            "invalid_tika_response",
                            "reader_result_size",
                            "reader_result_type",
                            "trailing_content",
                            "unexpected_character_after_quote",
                            "unexpected_eof",
                            "unexpected_quote",
                            "unterminated_quote",
                        }
                    ),
                ),
            ),
        )
    if error_code == "PARSER_UNSUPPORTED_FEATURE":
        return (
            "Parser не поддерживает обнаруженную возможность формата.",
            (
                (
                    "feature",
                    frozenset(
                        {
                            "ambiguous_dialect",
                            "configured_template",
                            "dialect_detection",
                        }
                    ),
                ),
                (
                    "reason",
                    frozenset({"feature_disabled", "insufficient_structure"}),
                ),
            ),
        )
    if error_code == "PARSER_ENCODING_UNSUPPORTED":
        return (
            "Parser не может безопасно определить или декодировать кодировку.",
            (
                (
                    "reason",
                    frozenset({"decode_failed", "undetermined", "unsupported_codec"}),
                ),
            ),
        )
    if error_code == "PARSER_DEPENDENCY_UNAVAILABLE":
        return (
            "Требуется optional parser extra.",
            (("extra", frozenset({"xml", "yaml", "excel", "pdf", "office", "tika"})),),
        )
    if error_code == "PARSER_TIKA_UNAVAILABLE":
        return ("Tika endpoint недоступен.", ())
    if error_code == "PARSER_NO_TEXT_LAYER":
        return ("PDF не содержит извлекаемого текстового слоя.", ())
    if error_code == "PROCESSING_TIMEOUT":
        return ("Истёк timeout parser.", ())
    return None


def _rebuild_allowed_parser_error(error: ParserError) -> ParserError | None:
    """Создать безопасный public outcome из минимальной части adapter error."""

    policy = _allowed_parser_error_policy(error.error_code)
    if policy is None:
        return None
    message, allowed_details = policy
    details: dict[str, str | int] = {}
    for key, allowed_values in allowed_details:
        value = error.details.get(key)
        if type(value) is str and value in allowed_values:
            details[key] = value
    for key in ("record_number", "line_number"):
        value = error.details.get(key)
        if type(value) is int and 1 <= value <= _MAX_SAFE_SOURCE_POSITION:
            details[key] = value
    return ParserError(
        error_code=error.error_code,
        message=message,
        details=details,
    )


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
            effective_context = context
            detected_encoding = self._probe_result.detected_encoding
            if (
                detected_encoding is not None
                and context.detected_encoding is not None
                and context.detected_encoding != detected_encoding
            ):
                raise _output_error(self.adapter_id, "context_encoding_identity")
            if detected_encoding is not None and context.detected_encoding is None:
                try:
                    effective_context = replace(
                        context,
                        detected_encoding=detected_encoding,
                    )
                except ValueError as error:
                    raise _output_error(
                        self.adapter_id,
                        "probe_encoding_invalid",
                        cause=error,
                    ) from None
            cancellation: asyncio.CancelledError | None = None
            grouped_failure: tuple[_BoundaryError, ...] = ()
            call_failure: ParserError | SecurityPolicyError | None = None
            try:
                returned: object = self._registration.parser.parse(
                    source,
                    effective_context,
                )
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
            except ParserError as error:
                call_failure = _rebuild_allowed_parser_error(error) or _output_error(
                    self.adapter_id,
                    "parse_call_failed",
                    cause=error,
                )
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
                max_physical_objects=context.max_physical_objects,
                max_batches=context.batch_options.max_batches,
                release=self._release_stream,
            )

        return self._create_stream(factory)

    def _parse_call_group_failure(
        self,
        error: BaseException,
    ) -> ParserError | SecurityPolicyError:
        if isinstance(error, SecurityPolicyError):
            return _detach_security_error(error)
        if isinstance(error, ParserError):
            allowed = _rebuild_allowed_parser_error(error)
            if allowed is not None:
                return allowed
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
        "_indexed_refs",
        "_iterator",
        "_max_batches",
        "_max_physical_objects",
        "_max_records",
        "_next_in_progress",
        "_next_index",
        "_operation_lock",
        "_physical_refs",
        "_registration",
        "_release",
        "_source",
        "_summaries",
        "_table_segments",
        "_terminal_seen",
        "_total_physical_objects",
        "_total_records",
        "_tracking_released",
        "_tree_segments",
    )

    def __init__(
        self,
        *,
        iterator: AsyncIterator[ExtractedBatch],
        source: SourceArtifact,
        registration: RegisteredParser,
        max_records: int,
        max_physical_objects: int,
        max_batches: int,
        release: _TrackStream,
    ) -> None:
        self._iterator = iterator
        self._source = source
        self._registration = registration
        self._max_records = max_records
        self._max_physical_objects = max_physical_objects
        self._max_batches = max_batches
        self._release = release
        self._operation_lock = asyncio.Lock()
        self._next_in_progress = False
        self._next_index = 0
        self._extraction_id: str | None = None
        self._fingerprints: set[str] = set()
        self._indexed_refs: list[PhysicalSourceRef] = []
        self._physical_refs: set[PhysicalSourceRef] = set()
        self._summaries: list[ExtractedBatchSummary] = []
        self._table_segments: dict[str, tuple[int, int, bool]] = {}
        self._tree_segments: dict[str, tuple[int, int, bool, str]] = {}
        self._total_records = 0
        self._total_physical_objects = 0
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
        except ParserError as error:
            iteration_failure = _rebuild_allowed_parser_error(error) or _output_error(
                self._registration.identity.adapter_id,
                "parse_iteration_failed",
                cause=error,
            )
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
        if self._next_index >= self._max_batches:
            await self._fail_limit_locked("batch_count", self._max_batches)
        preflight_failure: ParserError | None = None
        try:
            batch_counts = self._preflight_counts(item)
        except Exception as error:  # parser DTO является недоверенной boundary
            preflight_failure = _output_error(
                self._registration.identity.adapter_id,
                "batch_schema",
                cause=error,
            )
        if preflight_failure is not None:
            await self._fail_error_locked(preflight_failure)
        if batch_counts is _PreflightLimit.RECORDS:
            await self._fail_limit_locked(
                _PreflightLimit.RECORDS,
                self._max_records,
            )
        if batch_counts is _PreflightLimit.PHYSICAL_OBJECTS:
            await self._fail_limit_locked(
                _PreflightLimit.PHYSICAL_OBJECTS,
                self._max_physical_objects,
            )
        batch_record_count, batch_physical_count = batch_counts
        if batch_physical_count > (
            self._max_physical_objects - self._total_physical_objects
        ):
            await self._fail_limit_locked(
                "physical_objects",
                self._max_physical_objects,
            )
        validation_failure: ParserError | None = None
        try:
            item = self._revalidate_batch(item)
            self._validate_batch(item)
        except ParserError as error:
            validation_failure = error
        if validation_failure is not None:
            await self._raise_after_cleanup(validation_failure)

        if item.schema_version == "1.1.0":
            next_indexed_ref_count = len(self._indexed_refs) + len(item.indexed_refs)
            if next_indexed_ref_count > _MAX_INDEXED_REFS:
                await self._fail_limit_locked("indexed_refs", _MAX_INDEXED_REFS)
            if set(item.indexed_refs) & set(self._indexed_refs):
                await self._fail_locked("indexed_refs_duplicate")
            self._indexed_refs.extend(item.indexed_refs)

        summary = item.to_summary()
        if item.schema_version == "1.1.0":
            self._physical_refs.update(item.indexed_refs)
        else:
            self._physical_refs.update(item.physical_refs())
        self._summaries.append(summary)
        self._next_index += 1
        self._fingerprints.add(item.batch_fingerprint)
        self._total_records += batch_record_count
        self._total_physical_objects += batch_physical_count

        if item.is_last:
            manifest = item.manifest
            if manifest is None or manifest.batches != tuple(self._summaries):
                await self._fail_locked("manifest_sequence")
            if item.schema_version == "1.1.0" and (
                manifest.source_index.refs != tuple(self._indexed_refs)
            ):
                await self._fail_locked("manifest_source_index")
            if item.schema_version != "1.1.0" and not (
                set(manifest.source_index.refs) <= self._physical_refs
            ):
                await self._fail_locked("manifest_source_index")
            if any(not closed for _, _, closed in self._table_segments.values()):
                await self._fail_locked("table_segment_missing_terminal")
            if any(not closed for _, _, closed, _ in self._tree_segments.values()):
                await self._fail_locked("tree_segment_missing_terminal")
            self._terminal_seen = True
        return item

    def _iteration_group_failure(
        self,
        error: BaseException,
    ) -> ParserError | SecurityPolicyError:
        if isinstance(error, SecurityPolicyError):
            return _detach_security_error(error)
        if isinstance(error, ParserError):
            allowed = _rebuild_allowed_parser_error(error)
            if allowed is not None:
                return allowed
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

    def _preflight_counts(
        self,
        item: ExtractedBatch,
    ) -> tuple[int, int] | _PreflightLimit:
        """Посчитать logical и physical units до дорогой deep-copy."""

        remaining_records = self._max_records - self._total_records
        if item.record_count is not None and item.record_count > remaining_records:
            return _PreflightLimit.RECORDS

        remaining_physical = self._max_physical_objects - self._total_physical_objects
        physical_count = 0

        def include(quantity: int) -> bool:
            nonlocal physical_count
            if quantity > remaining_physical - physical_count:
                return False
            physical_count += quantity
            return True

        if not include(len(item.lines)):
            return _PreflightLimit.PHYSICAL_OBJECTS
        if not include(len(item.blocks)):
            return _PreflightLimit.PHYSICAL_OBJECTS
        if not include(len(item.tables)):
            return _PreflightLimit.PHYSICAL_OBJECTS
        if not include(len(item.trees)):
            return _PreflightLimit.PHYSICAL_OBJECTS
        for block in item.blocks:
            if not include(len(block.lines)):
                return _PreflightLimit.PHYSICAL_OBJECTS
        for table in item.tables:
            if not include(len(table.cells)):
                return _PreflightLimit.PHYSICAL_OBJECTS

        for line in item.lines:
            for value in line.values:
                if value.value_id is not None and not include(1):
                    return _PreflightLimit.PHYSICAL_OBJECTS
        for block in item.blocks:
            for value in block.values:
                if value.value_id is not None and not include(1):
                    return _PreflightLimit.PHYSICAL_OBJECTS
            for line in block.lines:
                for value in line.values:
                    if value.value_id is not None and not include(1):
                        return _PreflightLimit.PHYSICAL_OBJECTS
        for table in item.tables:
            for cell in table.cells:
                if cell.value.value_id is not None and not include(1):
                    return _PreflightLimit.PHYSICAL_OBJECTS
        for node in item.trees:
            if (
                node.value is not None
                and node.value.value_id is not None
                and not include(1)
            ):
                return _PreflightLimit.PHYSICAL_OBJECTS

        logical_count = (
            item.record_count
            if item.record_count is not None
            else max(physical_count, 1)
        )
        if logical_count > remaining_records:
            return _PreflightLimit.RECORDS
        return logical_count, physical_count

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
        if (
            item.schema_version == "1.1.0"
            and batch_fingerprint(item) != item.batch_fingerprint
        ):
            raise _output_error(identity.adapter_id, "batch_fingerprint_mismatch")
        if (
            item.schema_version == "1.1.0"
            and item.manifest is not None
            and manifest_fingerprint(item.manifest)
            != item.manifest.extraction_fingerprint
        ):
            raise _output_error(
                identity.adapter_id,
                "extraction_fingerprint_mismatch",
            )
        if self._extraction_id is None:
            self._extraction_id = item.extraction_id
        elif item.extraction_id != self._extraction_id:
            raise _output_error(identity.adapter_id, "extraction_identity")
        self._validate_table_segments(item)
        self._validate_tree_segments(item)

    def _validate_table_segments(self, item: ExtractedBatch) -> None:
        """Проверить непрерывность явно сегментированных physical tables."""

        adapter_id = self._registration.identity.adapter_id
        for table in item.tables:
            if table.segment_index is None:
                continue
            row_start = table.row_start_index
            row_end = table.row_end_index
            is_last = table.is_last_segment
            if row_start is None or row_end is None or is_last is None:
                raise _output_error(adapter_id, "table_segment_schema")
            previous = self._table_segments.get(table.table_id)
            if previous is None:
                if table.segment_index != 0:
                    raise _output_error(adapter_id, "table_segment_sequence")
            else:
                next_segment_index, next_row_index, closed = previous
                if closed:
                    raise _output_error(adapter_id, "table_segment_after_terminal")
                if (
                    table.segment_index != next_segment_index
                    or row_start != next_row_index
                ):
                    raise _output_error(adapter_id, "table_segment_sequence")
            self._table_segments[table.table_id] = (
                table.segment_index + 1,
                row_end + 1,
                is_last,
            )

    def _validate_tree_segments(self, item: ExtractedBatch) -> None:
        """Проверить continuation identity и child ranges physical trees."""

        adapter_id = self._registration.identity.adapter_id
        for node in item.trees:
            if node.tree_id is None:
                continue
            segment_index = node.segment_index
            child_start = node.child_start_index
            child_count = node.child_count
            is_last = node.is_last_segment
            node_kind = node.node_kind
            if (
                segment_index is None
                or child_start is None
                or child_count is None
                or is_last is None
                or node_kind is None
            ):
                raise _output_error(adapter_id, "tree_segment_schema")
            previous = self._tree_segments.get(node.tree_id)
            if previous is None:
                if segment_index != 0 or child_start != 0:
                    raise _output_error(adapter_id, "tree_segment_sequence")
            else:
                next_segment, next_child, closed, previous_kind = previous
                if closed:
                    raise _output_error(adapter_id, "tree_segment_after_terminal")
                if (
                    segment_index != next_segment
                    or child_start != next_child
                    or str(node_kind) != previous_kind
                ):
                    raise _output_error(adapter_id, "tree_segment_sequence")
            self._tree_segments[node.tree_id] = (
                segment_index + 1,
                child_start + child_count,
                is_last,
                str(node_kind),
            )

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

    async def _fail_limit_locked(self, resource: str, limit: int) -> Never:
        await self._raise_after_cleanup(
            SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED",
                message="Parser output превысил resource limit текущего ParseContext.",
                details={
                    "adapter_id": self._registration.identity.adapter_id,
                    "limit": limit,
                    "resource": resource,
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
