"""Отдельный доступ к reversible map; plaintext не является report или log DTO."""

from dataclasses import dataclass, field
from typing import Protocol

from structuraguard.contracts.privacy import PlaceholderHandle


@dataclass(frozen=True, slots=True)
class PlaceholderEntry:
    """Одна plaintext замена; repr скрыт, явное чтение value требует защиты host."""

    placeholder: str = field(repr=False)
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PlaceholderPayload:
    """Raw map и binding masked fields; не предназначены для prompt/report/logs."""

    redacted_fingerprint: str
    entries: tuple[PlaceholderEntry, ...] = field(repr=False)


class PlaceholderStore(Protocol):
    """Ciphertext-only retention, access/run binding, TTL и atomic bounded put.

    Principal поступает из trusted authentication host; handle не даёт authority.
    Реализация обязана скрывать raw errors, не оставлять plaintext при cancel и
    удалять просроченный ciphertext при purge/get. Crypto key хранится отдельно.
    """

    async def put(
        self,
        *,
        run_id: str,
        principal: str,
        payload: PlaceholderPayload,
        ttl_seconds: int,
    ) -> PlaceholderHandle:
        """Авторизовать writer/run и атомарно сохранить bounded ciphertext.

        Вернуть opaque handle с TTL. Недоступный ACL/cap/crypto даёт безопасный
        SecurityPolicyError; cancel не должен оставлять plaintext в store.
        """
        ...

    async def get(
        self, *, handle: PlaceholderHandle, run_id: str, principal: str
    ) -> PlaceholderPayload:
        """Вернуть plaintext после reader/run/TTL/tamper gates, иначе отказать.

        Handle не является bearer permission; principal аутентифицирует host.
        Expired ciphertext удаляется, diagnostics не содержат raw значения.
        """
        ...

    async def discard(
        self, *, handle: PlaceholderHandle, run_id: str, principal: str
    ) -> None:
        """Авторизовать writer/run и отозвать handle, удалив ciphertext.

        Неверный доступ даёт безопасный SecurityPolicyError; raw map не возвращается.
        """
        ...
