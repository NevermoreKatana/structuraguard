"""Bounded AES-GCM map store с явным ключом, run/principal allowlists и TTL."""

from __future__ import annotations

import json
import math
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from structuraguard.contracts.privacy import PlaceholderHandle, PlaceholderMapPolicy
from structuraguard.ports.privacy import PlaceholderEntry, PlaceholderPayload

from ._privacy import failure

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_TOKEN = re.compile(r"\[SGR:[0-9a-f]{32}:[0-9a-f]{4}\]", re.ASCII)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _uuid(value: str) -> str:
    if type(value) is not str or len(value) != 36:
        raise failure("SECURITY_MAP_ACCESS_DENIED") from None
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise failure("SECURITY_MAP_ACCESS_DENIED") from None
    return value


def _json_string_bytes(value: str, remaining: int) -> int:
    """Размер UTF-8 JSON string до кодирования plaintext и выделения buffer."""
    size = 2
    for char in value:
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            raise failure("SECURITY_MAP_INVALID") from None
        if char in '"\\\b\f\n\r\t':
            size += 2
        elif point < 32:
            size += 6
        else:
            size += (
                1 if point < 128 else 2 if point < 2048 else 3 if point < 65536 else 4
            )
        if size > remaining:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
    return size


@dataclass(frozen=True, slots=True)
class _Stored:
    handle: PlaceholderHandle
    deadline: float
    ciphertext: bytes = field(repr=False)
    nonce: bytes = field(repr=False)


class EncryptedMemoryPlaceholderStore:
    """Отдельный ephemeral ciphertext store; plaintext раскрывает только get.

    key: caller-owned 32 bytes AES-256 key, не читается из окружения/файла.
    policy: неизменяемые allowlists и конечные TTL/capacity. По умолчанию deny.
    Clock/monotonic нужны для deterministic tests; clock rollback закрывает доступ.
    Import optional cryptography происходит только при явном создании store.

    Raises:
        SecurityPolicyError: Неверный key/policy, SECURITY_CRYPTO_UNAVAILABLE без
            extra security. Put/get/discard дополнительно проверяют ACL/run/TTL,
            capacity и integrity; сообщения не содержат plaintext или key.

    Конструктор не выполняет network/filesystem I/O; put/get работают в памяти.
    UUID principal получает host из authentication, известный handle права не даёт.
    Put возвращает PlaceholderHandle, get — чувствительный PlaceholderPayload.
    Purge/discard/rotate_key/close явно удаляют ciphertext; фонового timer нет.
    Key остаётся в памяти, физическое стирание и zeroization не гарантируются.
    """

    def __init__(
        self,
        *,
        key: bytes,
        policy: PlaceholderMapPolicy,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if type(policy) is not PlaceholderMapPolicy or any(
            type(values) is not tuple
            or len(values) > 32
            or any(type(v) is not str or len(v) != 36 for v in values)
            for values in (policy.runs, policy.writers, policy.readers)
        ):
            raise failure("SECURITY_POLICY_INVALID") from None
        try:
            self._policy = PlaceholderMapPolicy.model_validate(
                policy.model_dump(warnings="error")
            )
        except (ValueError, TypeError):
            raise failure("SECURITY_POLICY_INVALID") from None
        self._cipher: AESGCM | None = self._new_cipher(key)
        self._clock, self._monotonic = clock, monotonic
        self._last_tick = float("-inf")
        self._last_now = datetime.min.replace(tzinfo=UTC)
        self._records: dict[str, _Stored] = {}
        self._bytes = 0
        self._times()

    @staticmethod
    def _new_cipher(key: bytes) -> AESGCM:
        if type(key) is not bytes or len(key) != 32:
            raise failure("SECURITY_MAP_KEY_INVALID") from None
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise failure("SECURITY_CRYPTO_UNAVAILABLE") from None
        return AESGCM(key)

    def _times(self) -> tuple[datetime, float]:
        now, tick = self._clock(), self._monotonic()
        if (
            self._cipher is None
            or type(now) is not datetime
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or now < self._last_now
            or type(tick) not in (int, float)
            or not math.isfinite(tick)
            or tick < self._last_tick
        ):
            raise failure("SECURITY_MAP_ACCESS_DENIED") from None
        self._last_now, self._last_tick = now, tick
        return now, tick

    def _authorize(self, principal: str, run_id: str, *, write: bool) -> None:
        if _uuid(run_id) not in self._policy.runs:
            raise failure("SECURITY_MAP_ACCESS_DENIED") from None
        selected = self._policy.writers if write else self._policy.readers
        if _uuid(principal) not in selected:
            raise failure("SECURITY_MAP_ACCESS_DENIED") from None

    def purge_expired(self) -> int:
        """Удалить истёкшие ciphertext; idle store требует явного host cleanup."""
        now, tick = self._times()
        ids = tuple(
            k
            for k, record in self._records.items()
            if tick >= record.deadline or now >= record.handle.expires_at
        )
        for key in ids:
            self._bytes -= len(self._records.pop(key).ciphertext)
        return len(ids)

    async def put(
        self,
        *,
        run_id: str,
        principal: str,
        payload: PlaceholderPayload,
        ttl_seconds: int,
    ) -> PlaceholderHandle:
        """Атомарно сохранить только ciphertext; capacity проверяется до вставки."""
        self._authorize(principal, run_id, write=True)
        self.purge_expired()
        now, tick = self._times()
        if (
            type(ttl_seconds) is not int
            or not 0 < ttl_seconds <= self._policy.max_ttl_seconds
        ):
            raise failure("SECURITY_REDACTION_DENIED") from None
        if len(self._records) >= self._policy.max_maps:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        if (
            type(payload) is not PlaceholderPayload
            or type(payload.entries) is not tuple
            or type(payload.redacted_fingerprint) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", payload.redacted_fingerprint)
            is None
        ):
            raise failure("SECURITY_MAP_INVALID") from None
        if len(payload.entries) > self._policy.max_entries:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        remaining = self._policy.max_bytes - self._bytes
        size = 16 + len(
            json.dumps([payload.redacted_fingerprint, []], separators=(",", ":"))
        )
        seen: set[str] = set()
        for index, entry in enumerate(payload.entries):
            if (
                type(entry) is not PlaceholderEntry
                or type(entry.placeholder) is not str
                or _TOKEN.fullmatch(entry.placeholder) is None
                or type(entry.value) is not str
                or entry.placeholder in seen
            ):
                raise failure("SECURITY_MAP_INVALID") from None
            seen.add(entry.placeholder)
            size += 3 + int(index > 0) + len(entry.placeholder) + 2
            size += _json_string_bytes(entry.value, remaining - size)
            if size > remaining:
                raise failure("SECURITY_LIMIT_EXCEEDED") from None
        try:
            encoded = json.dumps(
                [
                    payload.redacted_fingerprint,
                    [[e.placeholder, e.value] for e in payload.entries],
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (ValueError, UnicodeError):
            raise failure("SECURITY_MAP_INVALID") from None
        if len(encoded) + 16 > self._policy.max_bytes - self._bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED") from None
        handle = PlaceholderHandle(
            map_id=str(uuid4()),
            run_id=run_id,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        if handle.map_id in self._records:
            raise failure("SECURITY_MAP_INVALID") from None
        nonce = secrets.token_bytes(12)
        assert self._cipher is not None
        ciphertext = self._cipher.encrypt(nonce, encoded, self._aad(handle))
        self._records[handle.map_id] = _Stored(
            handle, tick + ttl_seconds, ciphertext, nonce
        )
        self._bytes += len(ciphertext)
        return handle

    def _aad(self, handle: PlaceholderHandle) -> bytes:
        return b"structuraguard-map-v1\0" + handle.canonical_json().encode("ascii")

    def _record(self, handle: PlaceholderHandle, run_id: str) -> _Stored:
        if (
            type(handle) is not PlaceholderHandle
            or handle.run_id != run_id
            or type(handle.map_id) is not str
        ):
            raise failure("SECURITY_MAP_ACCESS_DENIED") from None
        _uuid(handle.map_id)
        now, tick = self._times()
        record = self._records.get(handle.map_id)
        if record is None or record.handle != handle:
            raise failure("SECURITY_MAP_INVALID") from None
        if tick >= record.deadline or now >= handle.expires_at:
            self._bytes -= len(self._records.pop(handle.map_id).ciphertext)
            raise failure("SECURITY_MAP_EXPIRED") from None
        return record

    async def get(
        self, *, handle: PlaceholderHandle, run_id: str, principal: str
    ) -> PlaceholderPayload:
        """Расшифровать после principal/run/TTL gates; tamper не раскрывает plaintext."""
        from cryptography.exceptions import InvalidTag

        self._authorize(principal, run_id, write=False)
        record = self._record(handle, run_id)
        assert self._cipher is not None
        try:
            encoded = self._cipher.decrypt(
                record.nonce, record.ciphertext, self._aad(record.handle)
            )
            decoded: object = json.loads(encoded)
            if type(decoded) is not list or len(decoded) != 2:
                raise ValueError
            fingerprint, pairs = decoded
            if (
                type(fingerprint) is not str
                or type(pairs) is not list
                or len(pairs) > self._policy.max_entries
            ):
                raise ValueError
            entries = []
            for pair in pairs:
                if (
                    type(pair) is not list
                    or len(pair) != 2
                    or not all(type(v) is str for v in pair)
                ):
                    raise ValueError
                entries.append(PlaceholderEntry(pair[0], pair[1]))
            return PlaceholderPayload(fingerprint, tuple(entries))
        except (InvalidTag, ValueError, TypeError, UnicodeError):
            raise failure("SECURITY_MAP_INVALID") from None

    async def discard(
        self, *, handle: PlaceholderHandle, run_id: str, principal: str
    ) -> None:
        """Writer может явно отозвать map своего run; результат без raw metadata."""
        self._authorize(principal, run_id, write=True)
        record = self._record(handle, run_id)
        self._records.pop(record.handle.map_id)
        self._bytes -= len(record.ciphertext)

    def rotate_key(self, key: bytes) -> None:
        """Замена ключа отзывает все handles; не сохраняет старый key ring."""
        cipher = self._new_cipher(key)
        self._records.clear()
        self._bytes = 0
        self._cipher = cipher

    def close(self) -> None:
        """Отозвать handles и отпустить key/ciphertext; zeroization не обещается."""
        self._records.clear()
        self._bytes = 0
        self._cipher = None
