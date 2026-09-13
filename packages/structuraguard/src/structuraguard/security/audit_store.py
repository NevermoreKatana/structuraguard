"""Bounded memory audit store для тестов/локальных runs, без durable обещаний."""

import asyncio
from uuid import UUID

from pydantic import ValidationError

from structuraguard.contracts.audit import AuditEnvelope, AuditHead
from structuraguard.exceptions import SecurityPolicyError


def audit_failure(code: str) -> SecurityPolicyError:
    return SecurityPolicyError(error_code=code, message="Audit operation отклонена.")


def checked_envelope(value: AuditEnvelope, max_bytes: int = 16_384) -> AuditEnvelope:
    invalid = False
    try:
        if type(value) is not AuditEnvelope:
            raise ValueError
        # Закрытая schema имеет фиксированное число полей; прежде serialization
        # проверяем scalar shape, в том числе model_copy/model_construct forgery.
        value = AuditEnvelope.model_validate(value.model_dump(warnings="error"))
        if len(value.canonical_json().encode()) > max_bytes:
            raise ValueError
    except (ValueError, TypeError, ValidationError):
        invalid = True
    if invalid:
        raise audit_failure("AUDIT_EVENT_INVALID")
    return value


class MemoryAuditChainStore:
    """Хранить immutable envelopes с atomic CAS в одном event loop.

    max_events ограничивает весь store; max_event_bytes — один canonical envelope.
    Пределы проверяются до вставки. Неверные caps дают SecurityPolicyError/
    AUDIT_POLICY_INVALID. I/O нет; данные теряются при завершении процесса.
    Store не проверяет HMAC самостоятельно; caller использует AuditChain."""

    def __init__(
        self, *, max_events: int = 10_000, max_event_bytes: int = 16_384
    ) -> None:
        if (
            type(max_events) is not int
            or not 1 <= max_events <= 100_000
            or type(max_event_bytes) is not int
            or not 1 <= max_event_bytes <= 16_384
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        self._maximum, self._bytes = max_events, max_event_bytes
        self._records: dict[UUID, list[AuditEnvelope]] = {}
        self._ids: dict[tuple[UUID, UUID], AuditEnvelope] = {}
        self._lock = asyncio.Lock()

    async def tail(self, chain_id: UUID) -> AuditEnvelope | None:
        """Вернуть последний envelope цепочки chain_id или None без I/O."""
        records = self._records.get(chain_id, [])
        return records[-1] if records else None

    async def find(self, chain_id: UUID, event_id: UUID) -> AuditEnvelope | None:
        """Вернуть envelope по chain_id/event_id или None без раскрытия raw input."""
        return self._ids.get((chain_id, event_id))

    async def compare_append(
        self, envelope: AuditEnvelope, expected: AuditHead | None
    ) -> bool:
        """Атомарно сравнить expected head и добавить envelope; вернуть успех CAS.

        False означает конфликт. Invalid DTO/binding/cap даёт SecurityPolicyError;
        повтор того же event не создаёт дубликат. HMAC проверяет AuditChain, не store."""
        envelope = checked_envelope(envelope, self._bytes)
        async with self._lock:
            records = self._records.get(envelope.chain_id, [])
            tail = records[-1] if records else None
            if (tail.head if tail else None) != expected:
                return False
            if envelope.sequence != (
                tail.sequence + 1 if tail else 1
            ) or envelope.previous_hash != (tail.current_hash if tail else "0" * 64):
                raise audit_failure("AUDIT_CHAIN_INVALID")
            key = (envelope.chain_id, envelope.event.event_id)
            if key in self._ids:
                return False
            if len(self._ids) >= self._maximum:
                raise audit_failure("AUDIT_LIMIT_EXCEEDED")
            self._records.setdefault(envelope.chain_id, []).append(envelope)
            self._ids[key] = envelope
        return True

    async def read(
        self, chain_id: UUID, *, after: int, limit: int
    ) -> tuple[AuditEnvelope, ...]:
        """Вернуть ordered tuple для chain_id после sequence=after, максимум limit.

        After исключается, sequence начинается с 1. Неверные bounds дают
        SecurityPolicyError; read не подписывает и не подтверждает envelopes."""
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 256
        ):
            raise audit_failure("AUDIT_POLICY_INVALID")
        return tuple(self._records.get(chain_id, [])[after : after + limit])
