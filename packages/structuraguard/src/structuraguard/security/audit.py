"""HMAC-SHA-256 chain с bounded verification, rotation и store CAS."""

from __future__ import annotations

import asyncio
import hashlib
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from functools import partial
from types import MappingProxyType
from uuid import UUID

from structuraguard.contracts._base import canonical_json_value
from structuraguard.contracts.audit import (
    AuditEnvelope,
    AuditHead,
    AuditVerification,
    SecurityAuditEvent,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.audit import AuditChainStore, AuditKeyProvider, AuditSigner

from .audit_store import audit_failure, checked_envelope


def signing_bytes(envelope: AuditEnvelope) -> bytes:
    """v1: 32 raw previous bytes || UTF-8 canonical body без обоих hash fields."""
    body = canonical_json_value(
        envelope, exclude_top_level=frozenset({"previous_hash", "current_hash"})
    )
    return bytes.fromhex(envelope.previous_hash) + body.encode("utf-8")


async def _safe[T](operation: Callable[[], Awaitable[T]]) -> T:
    code = "AUDIT_OPERATION_FAILED"
    cancelled = False
    try:
        return await operation()
    except asyncio.CancelledError:
        cancelled = True
    except BaseException as error:
        # Foreign store/KMS — boundary; process-control исключения сохраняются.
        if isinstance(error, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        if isinstance(error, SecurityPolicyError) and error.error_code in {
            "AUDIT_CHAIN_INVALID",
            "AUDIT_EVENT_INVALID",
            "AUDIT_KEY_UNAVAILABLE",
            "AUDIT_LIMIT_EXCEEDED",
            "AUDIT_SCHEMA_INVALID",
            "AUDIT_PERMISSION_DENIED",
            "AUDIT_CHAIN_EMPTY",
            "AUDIT_PROCESSING_TIMEOUT",
            "AUDIT_DURABILITY_REQUIRED",
        }:
            code = error.error_code
    if cancelled:
        raise asyncio.CancelledError
    raise audit_failure(code)


class MemoryAuditKeys:
    """Хранить явно переданный immutable key ring без чтения окружения/файлов.

    keys — Mapping UUID к 32–64 bytes, от 1 до 32 записей. Неверный ring даёт
    SecurityPolicyError/AUDIT_POLICY_INVALID. Значения не публикуются в repr/DTO;
    key_for явно возвращает ключ. Защита памяти и zeroization не гарантированы."""

    def __init__(self, keys: Mapping[UUID, bytes]) -> None:
        if not 1 <= len(keys) <= 32 or any(
            type(key_id) is not UUID
            or type(key) is not bytes
            or not 32 <= len(key) <= 64
            for key_id, key in keys.items()
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        self._keys = MappingProxyType(dict(keys))

    async def key_for(self, key_id: UUID) -> bytes:
        """Вернуть bytes ключа по UUID; неизвестный ID даёт AUDIT_KEY_UNAVAILABLE."""
        key = self._keys.get(key_id)
        if key is None:
            raise audit_failure("AUDIT_KEY_UNAVAILABLE")
        return key


class HMACAuditSigner:
    """Подписывать canonical bytes через явный AuditKeyProvider.

    keys передаёт trusted host; конструктор не выполняет I/O. Sign может ожидать
    внешний KMS adapter. HMAC подтверждает владение ключом, не истинность события
    и не non-repudiation; raw ключи и diagnostics не публикуются."""

    def __init__(self, keys: AuditKeyProvider) -> None:
        self._keys = keys

    async def sign(self, key_id: UUID, message: bytes) -> bytes:
        """Вернуть 32 bytes HMAC-SHA-256 для message и UUID key_id.

        Message — точные bytes до 16 416 bytes. Превышение даёт SecurityPolicyError/
        AUDIT_LIMIT_EXCEEDED; отсутствие/неверный ключ — AUDIT_KEY_UNAVAILABLE.
        Чужие key-provider diagnostics санитизируются, отмена сохраняет CancelledError."""
        import hmac

        if type(message) is not bytes or len(message) > 16_416:
            raise audit_failure("AUDIT_LIMIT_EXCEEDED")
        key = await _safe(lambda: self._keys.key_for(key_id))
        if type(key) is not bytes or not 32 <= len(key) <= 64:
            raise audit_failure("AUDIT_KEY_UNAVAILABLE")
        return hmac.digest(key, message, hashlib.sha256)


class AuditChain:
    """Подписывать и проверять ограниченную цепочку одного run.

    Args:
        chain_id: UUID цепочки; не меняется при rotation.
        run_id: UUID run для проверки каждого event.
        key_id: Текущий UUID ключа подписи; прошлые verification keys сохраняет host.
        policy_fingerprint: SHA-256 trusted policy, общий для всей цепочки.
        signer: Явный AuditSigner без публикации key material.
        store: AuditChainStore с atomic CAS; durability зависит от backend.
        max_events: Максимум append/verification, от 1 до 100 000.
        timeout_seconds: Deadline операции, больше нуля и не более 300 секунд.

    Конструктор не выполняет I/O; неверная конфигурация даёт SecurityPolicyError/
    AUDIT_POLICY_INVALID. Методы обращаются к signer/store. Caller хранит binding
    и trusted anchor вне store. Без anchor не обнаруживается удалённый suffix;
    key holder может переподписать историю. HMAC не доказывает полноту lifecycle."""

    def __init__(
        self,
        *,
        chain_id: UUID,
        run_id: UUID,
        key_id: UUID,
        policy_fingerprint: str,
        signer: AuditSigner,
        store: AuditChainStore,
        max_events: int = 10_000,
        timeout_seconds: float = 10,
    ) -> None:
        if (
            any(type(value) is not UUID for value in (chain_id, run_id, key_id))
            or type(max_events) is not int
            or not 1 <= max_events <= 100_000
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        if (
            len(policy_fingerprint) != 71
            or not policy_fingerprint.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in policy_fingerprint[7:])
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        self._chain, self._run, self._key = chain_id, run_id, key_id
        self._policy, self._signer, self._store = policy_fingerprint, signer, store
        self._maximum, self._timeout = max_events, timeout_seconds

    async def _signature(self, envelope: AuditEnvelope) -> str:
        signature = await _safe(
            lambda: self._signer.sign(envelope.key_id, signing_bytes(envelope))
        )
        if type(signature) is not bytes or len(signature) != 32:
            raise audit_failure("AUDIT_KEY_UNAVAILABLE")
        return signature.hex()

    async def check_key(self) -> None:
        """Проверить key availability до DML; последующий отказ всё равно требует rollback."""

        async def operation() -> None:
            async with asyncio.timeout(self._timeout):
                value = await self._signer.sign(
                    self._key, b"StructuraGuard audit key availability v1"
                )
                if type(value) is not bytes or len(value) != 32:
                    raise audit_failure("AUDIT_KEY_UNAVAILABLE")

        await _safe(operation)

    def _binding(self, envelope: AuditEnvelope) -> None:
        if (
            envelope.chain_id != self._chain
            or envelope.event.run_id != self._run
            or envelope.event.policy_fingerprint != self._policy
        ):
            raise audit_failure("AUDIT_CHAIN_INVALID")

    async def _authenticate(self, envelope: AuditEnvelope) -> AuditEnvelope:
        import hmac

        envelope = checked_envelope(envelope)
        self._binding(envelope)
        if not hmac.compare_digest(
            await self._signature(envelope), envelope.current_hash
        ):
            raise audit_failure("AUDIT_CHAIN_INVALID")
        return envelope

    async def append(self, event: SecurityAuditEvent) -> AuditEnvelope:
        """Подписать event и вернуть атомарно добавленный AuditEnvelope.

        Проверяет DTO/binding до I/O. Повтор event_id идемпотентен только при том же body;
        до 16 CAS retries. Неверное evidence/signature даёт SecurityPolicyError, key/store
        failure санитизируется. Timeout/cancel не доказывает отсутствие внешней записи:
        повторяйте тот же event_id. Commit/rollback PostgreSQL store выполняет caller."""

        async def operation() -> AuditEnvelope:
            async with asyncio.timeout(self._timeout):
                # Валидация до первого обращения к key/store, включая forged DTO.
                candidate = checked_envelope(
                    AuditEnvelope.model_construct(
                        chain_id=self._chain,
                        sequence=1,
                        key_id=self._key,
                        event=event,
                        previous_hash="0" * 64,
                        current_hash="0" * 64,
                    )
                )
                self._binding(candidate)
                for _ in range(16):
                    previous = await _safe(
                        lambda: self._store.find(self._chain, event.event_id)
                    )
                    if previous is not None:
                        previous = await self._authenticate(previous)
                        if previous.event != candidate.event:
                            raise audit_failure("AUDIT_EVENT_INVALID")
                        return previous
                    tail = await _safe(lambda: self._store.tail(self._chain))
                    if tail is not None:
                        tail = await self._authenticate(tail)
                        if tail.sequence >= self._maximum:
                            raise audit_failure("AUDIT_LIMIT_EXCEEDED")
                        if tail.event.occurred_at > event.occurred_at or (
                            tail.event.target_id is not None
                            and tail.event.target_id != event.target_id
                        ):
                            raise audit_failure("AUDIT_EVENT_INVALID")
                    candidate = candidate.model_copy(
                        update={
                            "sequence": tail.sequence + 1 if tail else 1,
                            "previous_hash": tail.current_hash if tail else "0" * 64,
                        }
                    )
                    candidate = candidate.model_copy(
                        update={"current_hash": await self._signature(candidate)}
                    )
                    if await _safe(
                        partial(
                            self._store.compare_append,
                            candidate,
                            tail.head if tail else None,
                        )
                    ):
                        return candidate
                raise audit_failure("AUDIT_OPERATION_FAILED")

        return await _safe(operation)

    async def _verify(
        self, records: AsyncIterator[AuditEnvelope], expected_head: AuditHead | None
    ) -> AuditVerification:
        previous: AuditEnvelope | None = None
        seen: set[UUID] = set()
        deadline = asyncio.get_running_loop().time() + self._timeout
        async with asyncio.timeout(self._timeout):
            async for record in records:
                if asyncio.get_running_loop().time() >= deadline:
                    raise audit_failure("AUDIT_PROCESSING_TIMEOUT")
                if len(seen) >= self._maximum:
                    raise audit_failure("AUDIT_LIMIT_EXCEEDED")
                record = await self._authenticate(record)
                if (
                    record.sequence != (previous.sequence + 1 if previous else 1)
                    or record.previous_hash
                    != (previous.current_hash if previous else "0" * 64)
                    or record.event.event_id in seen
                ):
                    raise audit_failure("AUDIT_CHAIN_INVALID")
                if previous is not None and (
                    previous.event.occurred_at > record.event.occurred_at
                    or (
                        previous.event.target_id is not None
                        and previous.event.target_id != record.event.target_id
                    )
                ):
                    raise audit_failure("AUDIT_CHAIN_INVALID")
                seen.add(record.event.event_id)
                previous = record
            if previous is None:
                raise audit_failure("AUDIT_CHAIN_EMPTY")
            if expected_head is not None and previous.head != expected_head:
                raise audit_failure("AUDIT_CHAIN_INVALID")
            return AuditVerification(
                head=previous.head,
                count=len(seen),
                anchored=expected_head is not None,
                coverage="trusted_head" if expected_head else "provided_prefix",
            )

    async def verify_records(
        self,
        records: Iterable[AuditEnvelope],
        *,
        expected_head: AuditHead | None = None,
    ) -> AuditVerification:
        """Вернуть AuditVerification для bounded iterable envelopes от genesis.

        expected_head — независимый trusted anchor; без него проверяется лишь данный
        prefix. Подписывает verification bytes через signer, не читает store; N+1 item
        останавливает проверку без полного list. Tamper/empty/cap/failure дают
        SecurityPolicyError. Синхронно блокирующий iterator требует host isolation."""

        async def iterate() -> AsyncIterator[AuditEnvelope]:
            for record in records:
                yield record

        return await _safe(lambda: self._verify(iterate(), expected_head))

    async def verify(
        self, *, expected_head: AuditHead | None = None
    ) -> AuditVerification:
        """Прочитать store pages и вернуть AuditVerification до точного expected_head.

        Без anchor coverage=provided_prefix, а не доказательство полноты истории.
        Пустой store даёт AUDIT_CHAIN_EMPTY, mismatch/tamper — AUDIT_CHAIN_INVALID;
        остальные SecurityPolicyError скрывают backend diagnostics. Записей не делает."""

        async def iterate() -> AsyncIterator[AuditEnvelope]:
            after = 0
            while True:
                page = await _safe(
                    partial(self._store.read, self._chain, after=after, limit=256)
                )
                if type(page) is not tuple or len(page) > 256:
                    raise audit_failure("AUDIT_CHAIN_INVALID")
                if not page:
                    return
                for record in page:
                    yield record
                after += len(page)

        return await _safe(lambda: self._verify(iterate(), expected_head))

    async def verify_through(self, head: AuditHead) -> AuditVerification:
        """Проверить store prefix до trusted AuditHead и вернуть AuditVerification.

        Последующие события допустимы. Foreign/missing/tampered head даёт
        SecurityPolicyError/AUDIT_CHAIN_INVALID. Читает bounded pages и signer keys,
        не меняет store и не утверждает отсутствие событий после anchor."""
        if head.chain_id != self._chain or head.sequence > self._maximum:
            raise audit_failure("AUDIT_CHAIN_INVALID")

        async def iterate() -> AsyncIterator[AuditEnvelope]:
            after = 0
            while after < head.sequence:
                limit = min(256, head.sequence - after)
                page = await _safe(
                    partial(self._store.read, self._chain, after=after, limit=limit)
                )
                if type(page) is not tuple or len(page) > limit:
                    raise audit_failure("AUDIT_CHAIN_INVALID")
                if not page:
                    return
                for record in page:
                    yield record
                after += len(page)

        return await _safe(lambda: self._verify(iterate(), head))
