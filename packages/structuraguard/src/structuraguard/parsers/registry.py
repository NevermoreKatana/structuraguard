"""Instance-local registry технических parsers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Never, cast

from structuraguard.contracts.common import _safe_text
from structuraguard.contracts.plugins import (
    ParserDiscoveryPolicy,
    ParserPluginDescriptor,
)
from structuraguard.contracts.source import SourceArtifact
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.ports.parser import Parser
from structuraguard.ports.source import ProbeContext

from ._registration import ParserIdentity, RegisteredParser
from .discovery import (
    ParserPluginDiscoveryFailure,
    ParserPluginDiscoveryReport,
)
from .execution import (
    SelectedParser,
    ValidatedParserStream,
    _raise_detached_group,
    _raise_sanitized_cancelled,
    _sanitize_cancelled_error,
)

_ADAPTER_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_PYTHON_PATH_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)
_MIN_PRIORITY = -(2**31)
_MAX_PRIORITY = 2**31 - 1

type _CleanupOutcome = ParserError | asyncio.CancelledError


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserRegistrySnapshot:
    """Canonical read-only представление состояния registry.

    Attributes:
        parsers: Идентичности manual parsers в canonical ``adapter_id`` order.
        plugins: Обнаруженные декларативные plugin descriptors в canonical
            order.
        fingerprint: SHA-256 fingerprint identities и descriptor fingerprints.

    Snapshot не содержит mutable registry state и не вызывает parser либо
    plugin code при чтении. Fingerprint не хеширует parser code или
    байты plugin artifact.
    """

    parsers: tuple[ParserIdentity, ...]
    plugins: tuple[ParserPluginDescriptor, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _RegistryState:
    parsers: tuple[RegisteredParser, ...] = ()
    plugins: tuple[ParserPluginDescriptor, ...] = ()


def _invalid_adapter(reason: str, *, cause: BaseException | None = None) -> ParserError:
    return ParserError(
        error_code="PARSER_INVALID_ADAPTER",
        message="Parser не удовлетворяет обязательному registration contract.",
        details={"reason": reason},
        cause=cause,
    )


def _validate_adapter_id(value: object) -> str:
    if type(value) is not str:
        raise _invalid_adapter("adapter_id_type")
    if len(value) > 128 or _ADAPTER_ID_PATTERN.fullmatch(value) is None:
        raise _invalid_adapter("adapter_id_format")
    try:
        _safe_text(value)
    except ValueError as error:
        raise _invalid_adapter("adapter_id_safety", cause=error) from None
    return value


def _validate_version(value: object) -> str:
    if type(value) is not str:
        raise _invalid_adapter("version_type")
    if not value or len(value) > 128 or value != value.strip():
        raise _invalid_adapter("version_format")
    if any(
        ord(character) == 127
        or unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        for character in value
    ):
        raise _invalid_adapter("version_format")
    try:
        _safe_text(value)
    except ValueError as error:
        raise _invalid_adapter("version_safety", cause=error) from None
    return value


def _validate_priority(value: object) -> int:
    if type(value) is not int or not _MIN_PRIORITY <= value <= _MAX_PRIORITY:
        raise _invalid_adapter("priority_range")
    return value


def _snapshot_parser_class_target(parser: object) -> tuple[str, str] | None:
    parser_class = type(parser)
    # Built-in access обходит mutable ``__getattribute__`` custom metaclass и
    # не выполняет его код при снятии необязательного discovery target.
    namespace = type.__getattribute__(parser_class, "__dict__")
    module = namespace.get("__module__")
    qualname = type.__getattribute__(parser_class, "__qualname__")
    if type(module) is not str or type(qualname) is not str:
        return None
    if len(module) + 1 + len(qualname) > 512:
        return None
    if (
        _PYTHON_PATH_PATTERN.fullmatch(module) is None
        or _PYTHON_PATH_PATTERN.fullmatch(qualname) is None
    ):
        return None
    try:
        _safe_text(module)
        _safe_text(qualname)
    except ValueError:
        return None
    return (module, qualname)


def _snapshot_parser(parser: object) -> RegisteredParser:
    inspection_failure: ParserError | None = None
    try:
        conforms = isinstance(parser, Parser)
    except Exception as error:
        inspection_failure = _invalid_adapter("protocol_inspection", cause=error)
        conforms = False
    if inspection_failure is not None:
        raise inspection_failure from None
    if not conforms:
        raise _invalid_adapter("protocol_members")

    # Runtime-checkable Protocol сузил boundary object; дальнейшие значения всё
    # равно проверяются как недоверенные exact types.
    checked = cast(Parser, parser)
    identity_failure: ParserError | None = None
    try:
        adapter_id = _validate_adapter_id(checked.adapter_id)
        version = _validate_version(checked.version)
        priority = _validate_priority(checked.priority)
        probe = checked.probe
        parse = checked.parse
    except ParserError as error:
        identity_failure = error
    except Exception as error:
        identity_failure = _invalid_adapter("identity_access", cause=error)
    if identity_failure is not None:
        identity_failure.__context__ = None
        identity_failure.__cause__ = None
        identity_failure.__traceback__ = None
        raise identity_failure from None
    if not callable(probe) or not callable(parse):
        raise _invalid_adapter("method_not_callable")

    return RegisteredParser(
        identity=ParserIdentity(
            adapter_id=adapter_id,
            version=version,
            priority=priority,
        ),
        parser=checked,
        parser_class_target=_snapshot_parser_class_target(parser),
    )


def _registry_fingerprint(state: _RegistryState) -> str:
    payload = {
        "parsers": [
            {
                "adapter_id": item.identity.adapter_id,
                "version": item.identity.version,
                "priority": item.identity.priority,
                "origin": item.identity.origin,
            }
            for item in state.parsers
        ],
        "plugins": [item.metadata_fingerprint for item in state.plugins],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class ParserRegistry:
    """Владеет registrations одного SDK instance без module-level state.

    Каждый экземпляр создаётся пустым и изменяется только явными вызовами
    registration или discovery. Registry не сканирует entry points при
    создании и не разделяет mutable state с другими экземплярами.

    Security:
        Manual parser object считается trusted in-process кодом composition
        owner. Недоверенный plugin представлен только descriptor и не
        активируется в M3 без изолированного runner.
    """

    __slots__ = ("_active_sessions", "_lock", "_state")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = _RegistryState()
        self._active_sessions = 0

    def register(self, parser: Parser) -> ParserRegistrySnapshot:
        """Атомарно зарегистрировать trusted parser object.

        Args:
            parser: Уже импортированный и созданный technical parser.

        Returns:
            Snapshot registry после успешной регистрации.

        Raises:
            ParserError: Parser не соответствует contract, конфликтует с
                существующей registration либо registry заморожен active
                session.

        Side Effects:
            При успехе заменяет instance-local state новым canonical snapshot.
            При ошибке состояние не меняется.

        Security:
            Проверка identity обращается к properties объекта и тем самым может
            выполнить его код. Caller должен регистрировать только trusted
            in-process adapters.
        """

        return self.register_many((parser,))

    def register_many(self, parsers: Iterable[Parser]) -> ParserRegistrySnapshot:
        """Атомарно зарегистрировать набор trusted parser objects.

        Args:
            parsers: Caller-owned iterable уже созданных technical parsers.

        Returns:
            Snapshot registry со всем набором новых registrations.

        Raises:
            ParserError: Хотя бы один parser не соответствует contract,
                обнаружен duplicate либо registry заморожен active session.
            Exception: Исключение caller-provided iterable передаётся без
                замены.

        Side Effects:
            Материализует iterable и только после полной проверки атомарно
            заменяет instance-local state. Partial commit отсутствует.

        Security:
            Итерирование и чтение identity могут выполнять caller-owned код.
            Метод предназначен только для trusted in-process adapters.
        """

        # Freeze проверяется до чтения недоверенных properties кандидатов и
        # повторно перед commit, чтобы не было TOCTOU между threads.
        with self._lock:
            self._ensure_mutable()
        candidates = tuple(_snapshot_parser(parser) for parser in parsers)
        self._validate_candidate_duplicates(candidates)
        with self._lock:
            self._ensure_mutable()
            self._validate_against_state(candidates)
            self._state = _RegistryState(
                parsers=tuple(
                    sorted(
                        (*self._state.parsers, *candidates),
                        key=lambda item: item.identity.adapter_id,
                    )
                ),
                plugins=self._state.plugins,
            )
            return self._snapshot_unlocked()

    def snapshot(self) -> ParserRegistrySnapshot:
        """Вернуть immutable canonical snapshot текущего registry.

        Returns:
            Отсоединённые identities, descriptors и fingerprint в canonical
            order.

        Side Effects:
            Не изменяет registry и не вызывает parser либо plugin code.
        """

        with self._lock:
            return self._snapshot_unlocked()

    def session(self) -> ParserRegistrySession:
        """Создать snapshot lease для deterministic selection и parsing.

        Returns:
            Одноразовый async context manager. Lease открывается только при
            входе через ``async with``.

        Side Effects:
            Сам вызов не замораживает registry. Вход в context запрещает
            mutation до завершения cleanup всех созданных parser streams.
        """

        return ParserRegistrySession(self)

    def activate_plugin(self, adapter_id: str) -> Never:
        """Отклонить activation plugin до появления sandbox runner.

        Args:
            adapter_id: Canonical ID ранее обнаруженного descriptor.

        Raises:
            ParserError: ID неканоничен либо descriptor не найден.
            SecurityPolicyError: Descriptor найден, но безопасный runner в M3
                отсутствует; код ``SECURITY_SANDBOX_REQUIRED``.

        Side Effects:
            Не импортирует target, не вызывает ``EntryPoint.load()`` и не
            изменяет registry.

        Security:
            In-process fallback намеренно отсутствует.
        """

        canonical_id = _validate_adapter_id(adapter_id)
        with self._lock:
            known = any(
                descriptor.adapter_id == canonical_id
                for descriptor in self._state.plugins
            )
        if not known:
            raise ParserError(
                error_code="PARSER_NOT_FOUND",
                message="Plugin descriptor не найден в registry.",
                details={"adapter_id": canonical_id},
            )
        raise SecurityPolicyError(
            error_code="SECURITY_SANDBOX_REQUIRED",
            message="Недоверенный parser plugin требует изолированный runner.",
            details={"adapter_id": canonical_id},
        )

    def discover_plugins(
        self,
        policy: ParserDiscoveryPolicy,
    ) -> ParserPluginDiscoveryReport:
        """Явно найти и зарегистрировать безопасные plugin descriptors.

        Args:
            policy: Exact runtime policy с непустым allowlist distributions и
                общим пределом принятых entry points.

        Returns:
            Отчёт с атомарно зарегистрированными descriptors и
            санитизированными failures. Ошибка одного distribution не отменяет
            регистрацию остальных валидных descriptors.

        Raises:
            ParserError: Policy не прошла runtime validation либо registry
                заморожен active session.

        Side Effects:
            Читает metadata только allowlisted installed distributions и при
            успехе изменяет instance-local registry. Plugin code не
            импортируется и не выполняется.

        Security:
            Metadata считается недоверенной, читается с byte limits и
            нормализуется в declarative descriptors. Host metadata finder
            остаётся доверенной частью composition environment.
        """

        from .discovery import discover_parser_plugins

        with self._lock:
            self._ensure_mutable()
        discovered = discover_parser_plugins(policy)

        accepted: list[ParserPluginDescriptor] = []
        failures = list(discovered.failures)
        with self._lock:
            self._ensure_mutable()
            existing_ids = {
                item.identity.adapter_id for item in self._state.parsers
            } | {item.adapter_id for item in self._state.plugins}
            existing_targets = {
                (item.module, item.attribute) for item in self._state.plugins
            }
            manual_targets = {
                item.parser_class_target
                for item in self._state.parsers
                if item.parser_class_target is not None
            }
            for descriptor in discovered.descriptors:
                duplicate_kind: str | None = None
                if descriptor.adapter_id in existing_ids:
                    duplicate_kind = "adapter_id"
                elif (descriptor.module, descriptor.attribute) in (
                    existing_targets | manual_targets
                ):
                    duplicate_kind = "parser_class"
                if duplicate_kind is not None:
                    failures.append(
                        ParserPluginDiscoveryFailure(
                            distribution_name=descriptor.distribution_name,
                            adapter_id=descriptor.adapter_id,
                            error=self._duplicate_error(
                                duplicate_kind,
                                descriptor.adapter_id,
                            ),
                        )
                    )
                    continue
                accepted.append(descriptor)
                existing_ids.add(descriptor.adapter_id)
                existing_targets.add((descriptor.module, descriptor.attribute))

            if accepted:
                self._state = _RegistryState(
                    parsers=self._state.parsers,
                    plugins=tuple(
                        sorted(
                            (*self._state.plugins, *accepted),
                            key=lambda item: (
                                item.adapter_id,
                                item.distribution_name,
                                item.distribution_version,
                            ),
                        )
                    ),
                )

        def failure_key(
            failure: ParserPluginDiscoveryFailure,
        ) -> tuple[str, str, str, str, str]:
            reason = failure.error.details.get("reason")
            return (
                failure.distribution_name,
                failure.adapter_id or "",
                failure.error.error_code,
                reason if isinstance(reason, str) else "",
                failure.error.cause or "",
            )

        return ParserPluginDiscoveryReport(
            descriptors=tuple(accepted),
            failures=tuple(sorted(failures, key=failure_key)),
        )

    def _open_session(self) -> tuple[RegisteredParser, ...]:
        with self._lock:
            self._active_sessions += 1
            return self._state.parsers

    def _close_session(self) -> None:
        with self._lock:
            if self._active_sessions <= 0:
                raise RuntimeError("Parser registry session counter повреждён")
            self._active_sessions -= 1

    def _snapshot_unlocked(self) -> ParserRegistrySnapshot:
        state = self._state
        return ParserRegistrySnapshot(
            parsers=tuple(item.identity for item in state.parsers),
            plugins=state.plugins,
            fingerprint=_registry_fingerprint(state),
        )

    def _ensure_mutable(self) -> None:
        if self._active_sessions:
            raise ParserError(
                error_code="PARSER_REGISTRY_FROZEN",
                message="Registry нельзя изменять во время active parser session.",
                details={"active_sessions": self._active_sessions},
            )

    @staticmethod
    def _validate_candidate_duplicates(
        candidates: tuple[RegisteredParser, ...],
    ) -> None:
        seen_ids: set[str] = set()
        seen_instances: set[int] = set()
        for candidate in candidates:
            adapter_id = candidate.identity.adapter_id
            if adapter_id in seen_ids:
                raise ParserRegistry._duplicate_error("adapter_id", adapter_id)
            instance_id = id(candidate.parser)
            if instance_id in seen_instances:
                raise ParserRegistry._duplicate_error("parser_instance", adapter_id)
            seen_ids.add(adapter_id)
            seen_instances.add(instance_id)

    def _validate_against_state(
        self,
        candidates: tuple[RegisteredParser, ...],
    ) -> None:
        existing_ids = {item.identity.adapter_id for item in self._state.parsers} | {
            item.adapter_id for item in self._state.plugins
        }
        plugin_targets = {(item.module, item.attribute) for item in self._state.plugins}
        for candidate in candidates:
            adapter_id = candidate.identity.adapter_id
            if adapter_id in existing_ids:
                raise self._duplicate_error("adapter_id", adapter_id)
            if any(item.parser is candidate.parser for item in self._state.parsers):
                raise self._duplicate_error("parser_instance", adapter_id)
            if (
                candidate.parser_class_target is not None
                and candidate.parser_class_target in plugin_targets
            ):
                raise self._duplicate_error("parser_class", adapter_id)

    @staticmethod
    def _duplicate_error(duplicate_kind: str, adapter_id: str) -> ParserError:
        return ParserError(
            error_code="PARSER_DUPLICATE_REGISTRATION",
            message="Parser registration конфликтует с существующей identity.",
            details={
                "adapter_id": adapter_id,
                "duplicate_kind": duplicate_kind,
            },
        )


class ParserRegistrySession:
    """Удерживает frozen набор parsers до закрытия созданных streams.

    Args:
        registry: Instance-local registry, у которого session арендует
            immutable snapshot.

    Session одноразова и предназначена для ``async with``. Вход замораживает
    mutation registry; выход закрывает streams, дожидается cleanup даже при
    cancellation и освобождает lease.

    Security:
        ``select()`` и parsing запускают только заранее зарегистрированный
        trusted parser code. Session не активирует plugin descriptors.
    """

    __slots__ = (
        "_closing",
        "_entered",
        "_lease_released",
        "_lock",
        "_open",
        "_parsers",
        "_registry",
        "_selection_lock",
        "_streams",
    )

    def __init__(self, registry: ParserRegistry) -> None:
        self._registry = registry
        self._lock = threading.RLock()
        self._selection_lock = asyncio.Lock()
        self._parsers: tuple[RegisteredParser, ...] = ()
        self._streams: dict[ValidatedParserStream, None] = {}
        self._closing = False
        self._entered = False
        self._lease_released = False
        self._open = False

    async def __aenter__(self) -> ParserRegistrySession:
        """Открыть одноразовую session и зафиксировать parser snapshot.

        Returns:
            Эту session, готовую для ``select()``.

        Raises:
            ParserError: Session уже входила в context.

        Side Effects:
            Увеличивает lease counter registry и запрещает его mutation.
        """

        with self._lock:
            if self._entered:
                raise ParserError(
                    error_code="PARSER_SESSION_CLOSED",
                    message=("Parser registry session одноразова и уже использована."),
                )
            self._entered = True
            self._parsers = self._registry._open_session()
            self._open = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        """Закрыть session, quarantined streams и освободить registry lease.

        Args:
            exc_type: Тип исключения из тела context либо ``None``.
            exc_value: Исключение из тела context либо ``None``.
            traceback: Traceback исключения из тела context либо ``None``.

        Raises:
            asyncio.CancelledError: После обязательного cleanup, если выход был
                отменён и других ошибок нет.
            ParserError: Единственный cleanup parser stream завершился ошибкой,
                а тело context не содержит другого исключения.
            BaseExceptionGroup: Тело context либо cancellation совпали с
                cleanup failure или несколько streams завершили cleanup с
                ошибками. Группа сохраняет исходный outcome и каждую
                санитизированную parser error отдельными элементами; её
                собственная неявная exception chain очищается.

        Side Effects:
            Запрещает новые operations, закрывает созданные streams и
            освобождает registry lease после их cleanup.
        """

        with self._lock:
            self._open = False
            self._parsers = ()
            self._closing = True
            # Quarantine синхронна: in-flight read не может выдать batch в
            # промежутке до запуска deferred async cleanup.
            for stream in self._streams:
                stream._begin_close()

        try:
            cleanup_outcomes = await self._cleanup_deferred()
        finally:
            self._release_lease_if_quiescent()

        if isinstance(exc_value, asyncio.CancelledError):
            if not cleanup_outcomes:
                _raise_sanitized_cancelled(exc_value)
            body_outcomes: tuple[BaseException, ...] = (
                _sanitize_cancelled_error(exc_value),
            )
        else:
            body_outcomes = (exc_value,) if exc_value is not None else ()
        if not cleanup_outcomes:
            return
        outcomes = body_outcomes + cleanup_outcomes
        if len(outcomes) == 1:
            raise outcomes[0] from None
        _raise_detached_group(
            "Parser session завершилась с несколькими ошибками.",
            outcomes,
        )

    async def select(
        self,
        source: SourceArtifact,
        context: ProbeContext,
    ) -> SelectedParser:
        """Детерминированно выбрать parser из frozen session snapshot.

        Args:
            source: Метаданные immutable source snapshot с недоверенными hints.
            context: Fingerprint-bound bounded reader для ``probe()``.

        Returns:
            Session-bound handle выбранного parser и его validated
            ``ProbeResult``.

        Raises:
            asyncio.CancelledError: Если selection отменён; payload, notes и
                exception chain cancellation удаляются.
            BaseExceptionGroup: Если parser вернул grouped failure; leaves и
                exception chain санитизированы.
            ParserError: Session закрыта, probe завершился ошибкой, результат
                probe невалиден, формат не поддержан или strong evidence
                неоднозначна.
            SecurityPolicyError: Parser сообщил security policy failure.

        Side Effects:
            Последовательно вызывает ``probe()`` trusted parsers в canonical
            ``adapter_id`` order. Registry state не изменяется.

        Security:
            MIME и extension являются hints и сами не подтверждают формат.
            Parser получает только source metadata и bounded reader, без LLM
            или DB authority.
        """

        self._ensure_open()
        from .selection import select_parser

        try:
            async with self._selection_lock:
                self._ensure_open()
                selected = await select_parser(self._parsers, source, context)
                self._ensure_open()
        except asyncio.CancelledError as error:
            _raise_sanitized_cancelled(error)
        return SelectedParser(
            registration=selected.registration,
            probe_result=selected.probe_result,
            source=source,
            create_stream=self._create_stream,
            release_stream=self._release_stream,
        )

    def _ensure_open(self) -> None:
        with self._lock:
            if not self._open:
                raise ParserError(
                    error_code="PARSER_SESSION_CLOSED",
                    message="Parser session закрыта или ещё не открыта.",
                )

    def _create_stream(
        self,
        factory: Callable[[], ValidatedParserStream],
    ) -> ValidatedParserStream:
        with self._lock:
            if not self._open:
                raise ParserError(
                    error_code="PARSER_SESSION_CLOSED",
                    message="Parser session закрыта или ещё не открыта.",
                )
            stream = factory()
            self._streams[stream] = None
            return stream

    def _release_stream(self, stream: ValidatedParserStream) -> None:
        with self._lock:
            self._streams.pop(stream, None)
        self._release_lease_if_quiescent()

    async def _close_streams(
        self,
        streams: tuple[ValidatedParserStream, ...],
    ) -> tuple[_CleanupOutcome, ...]:
        outcomes: list[_CleanupOutcome] = []
        for stream in streams:
            try:
                await stream.aclose()
            except BaseExceptionGroup as error:
                outcomes.extend(stream._normalize_cleanup_failure(error))
            except (asyncio.CancelledError, ParserError) as error:
                outcomes.extend(stream._normalize_cleanup_failure(error))
        return tuple(outcomes)

    async def _cleanup(
        self,
    ) -> tuple[_CleanupOutcome, ...]:
        async with self._selection_lock:
            with self._lock:
                streams = tuple(self._streams)
            return await self._close_streams(streams)

    async def _cleanup_deferred(
        self,
    ) -> tuple[_CleanupOutcome, ...]:
        cleanup = asyncio.create_task(self._cleanup())
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                outcomes = await asyncio.shield(cleanup)
                prefix = (cancellation,) if cancellation is not None else ()
                return (*prefix, *outcomes)
            except asyncio.CancelledError as error:
                if cancellation is None:
                    cancellation = _sanitize_cancelled_error(error)
                if cleanup.cancelled():
                    return (cancellation,)

    def _release_lease_if_quiescent(self) -> None:
        release_lease = False
        with self._lock:
            if self._closing and not self._streams and not self._lease_released:
                self._lease_released = True
                release_lease = True
        if release_lease:
            self._registry._close_session()
