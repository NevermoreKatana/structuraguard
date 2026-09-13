"""Явные key, signature и atomic persistence ports; никаких credentials в DTO."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from structuraguard.contracts.audit import AuditEnvelope, AuditHead


@runtime_checkable
class AuditKeyProvider(Protocol):
    """Получать ключи по trusted UUID; I/O и защита key material принадлежат host."""

    async def key_for(self, key_id: UUID) -> bytes:
        """Вернуть HMAC key для explicit ID; неизвестный ID требует отказа."""


@runtime_checkable
class AuditSigner(Protocol):
    """Подписывать canonical bytes без публикации ключа и foreign diagnostics."""

    async def sign(self, key_id: UUID, message: bytes) -> bytes:
        """Подписать bounded canonical bytes; результат ровно 32 bytes."""


@runtime_checkable
class AuditChainStore(Protocol):
    """Store обязан атомарно сравнивать tail и добавлять immutable envelope.

    ACL/retention/durability принадлежат backend. Read ограничен page size;
    SQL implementation проверяет длину payload до передачи его driver.
    """

    async def tail(self, chain_id: UUID) -> AuditEnvelope | None:
        """Вернуть последний committed envelope выбранной цепочки."""

    async def find(self, chain_id: UUID, event_id: UUID) -> AuditEnvelope | None:
        """Найти event для idempotent retry; другой body требует отказа."""

    async def compare_append(
        self, envelope: AuditEnvelope, expected: AuditHead | None
    ) -> bool:
        """Atomic CAS; False означает конфликт, исключение означает отказ store."""

    async def read(
        self, chain_id: UUID, *, after: int, limit: int
    ) -> tuple[AuditEnvelope, ...]:
        """Вернуть ordered page; sequence начинается с 1, after исключается."""
