import asyncio
import hashlib
import hmac
import json
import traceback
from datetime import UTC, datetime
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from structuraguard.contracts.audit import AuditEnvelope, AuditKind, SecurityAuditEvent
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security.audit import (
    AuditChain,
    HMACAuditSigner,
    MemoryAuditKeys,
    signing_bytes,
)
from structuraguard.security.audit_store import MemoryAuditChainStore

KEY = UUID(int=1)
CHAIN = UUID(int=2)
RUN = UUID(int=3)


def event(number: int = 1) -> SecurityAuditEvent:
    return SecurityAuditEvent(
        event_id=UUID(int=number + 100),
        run_id=RUN,
        actor_id=UUID(int=4),
        policy_fingerprint="sha256:" + "a" * 64,
        occurred_at=datetime(2026, 9, 13, tzinfo=UTC),
        kind=AuditKind.RUN_STARTED,
    )


def chain(store: MemoryAuditChainStore | None = None) -> AuditChain:
    return AuditChain(
        chain_id=CHAIN,
        run_id=RUN,
        key_id=KEY,
        policy_fingerprint=event().policy_fingerprint,
        signer=HMACAuditSigner(MemoryAuditKeys({KEY: b"k" * 32})),
        store=store or MemoryAuditChainStore(),
    )


@pytest.mark.anyio
async def test_append_verify_replay_and_truncation() -> None:
    audit = chain()
    first = await audit.append(event())
    assert await audit.append(event()) == first
    second = await audit.append(event(2))
    verified = await audit.verify(expected_head=second.head)
    assert verified.anchored and verified.count == 2
    with pytest.raises(SecurityPolicyError, match="AUDIT_CHAIN_INVALID"):
        await audit.verify_records((first,), expected_head=second.head)


@pytest.mark.anyio
async def test_mutation_is_detected() -> None:
    audit = chain()
    first = await audit.append(event())
    altered = first.model_copy(update={"event": event(2)})
    with pytest.raises(SecurityPolicyError, match="AUDIT_CHAIN_INVALID"):
        await audit.verify_records((altered,))


@pytest.mark.anyio
async def test_empty_history_is_not_verified() -> None:
    with pytest.raises(SecurityPolicyError, match="AUDIT_CHAIN_EMPTY"):
        await chain().verify()


@pytest.mark.anyio
async def test_hmac_known_vector_and_closed_payload() -> None:
    record = await chain().append(event())
    body = record.model_dump(mode="json", exclude={"previous_hash", "current_hash"})
    expected = (
        b"\0" * 32
        + json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    )
    assert signing_bytes(record) == expected
    assert (
        record.current_hash == hmac.new(b"k" * 32, expected, hashlib.sha256).hexdigest()
    )
    with pytest.raises(ValidationError) as error:
        SecurityAuditEvent.model_validate(
            {**event().model_dump(), "payload": "restricted-canary"}
        )
    assert "restricted-canary" not in str(error.value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "reorder",
        "delete",
        "duplicate",
        "splice",
        "sequence",
        "key",
        "version",
        "algorithm",
        "previous",
    ],
)
async def test_chain_tampering(mutation: str) -> None:
    audit = chain()
    records = [await audit.append(event(i)) for i in range(3)]
    if mutation == "reorder":
        records[0], records[1] = records[1], records[0]
    elif mutation == "delete":
        records.pop(1)
    elif mutation == "duplicate":
        records.insert(1, records[0])
    else:
        changes: dict[str, dict[str, object]] = {
            "splice": {"chain_id": UUID(int=9)},
            "sequence": {"sequence": 8},
            "key": {"key_id": UUID(int=9)},
            "version": {"version": 2},
            "algorithm": {"algorithm": "sha256"},
            "previous": {"previous_hash": "f" * 64},
        }
        records[1] = records[1].model_copy(update=changes[mutation])
    with pytest.raises(SecurityPolicyError):
        await audit.verify_records(records)


@pytest.mark.anyio
async def test_rotation_retains_old_verification_keys() -> None:
    store = MemoryAuditChainStore()
    first = await chain(store).append(event())
    keys = MemoryAuditKeys({KEY: b"k" * 32, UUID(int=8): b"n" * 32})
    rotated = AuditChain(
        chain_id=CHAIN,
        run_id=RUN,
        key_id=UUID(int=8),
        policy_fingerprint=event().policy_fingerprint,
        signer=HMACAuditSigner(keys),
        store=store,
    )
    last = await rotated.append(event(2))
    assert last.previous_hash == first.current_hash and last.key_id != first.key_id
    assert (await rotated.verify(expected_head=last.head)).count == 2
    missing = AuditChain(
        chain_id=CHAIN,
        run_id=RUN,
        key_id=UUID(int=8),
        policy_fingerprint=event().policy_fingerprint,
        signer=HMACAuditSigner(MemoryAuditKeys({UUID(int=8): b"n" * 32})),
        store=store,
    )
    with pytest.raises(SecurityPolicyError, match="AUDIT_KEY_UNAVAILABLE"):
        await missing.verify()


@pytest.mark.anyio
async def test_concurrent_cas_retry_and_conflicting_event_id() -> None:
    class YieldingStore(MemoryAuditChainStore):
        async def tail(self, chain_id: UUID) -> AuditEnvelope | None:
            value = await super().tail(chain_id)
            await asyncio.sleep(0)
            return value

    store = YieldingStore()
    records = await asyncio.gather(*(chain(store).append(event(i)) for i in range(8)))
    assert len({r.sequence for r in records}) == 8
    assert (await chain(store).verify()).count == 8
    with pytest.raises(SecurityPolicyError, match="AUDIT_EVENT_INVALID"):
        await chain(store).append(event().model_copy(update={"actor_id": UUID(int=77)}))


@pytest.mark.anyio
@given(st.integers(min_value=0, max_value=63))
async def test_any_changed_digest_nibble_fails(index: int) -> None:
    audit = chain()
    record = await audit.append(event())
    digest = record.current_hash
    changed = (
        digest[:index] + ("0" if digest[index] != "0" else "1") + digest[index + 1 :]
    )
    with pytest.raises(SecurityPolicyError, match="AUDIT_CHAIN_INVALID"):
        await audit.verify_records(
            (record.model_copy(update={"current_hash": changed}),)
        )


@pytest.mark.anyio
async def test_store_limit_boundary_and_one_over() -> None:
    audit = chain(MemoryAuditChainStore(max_events=1))
    await audit.append(event())
    with pytest.raises(SecurityPolicyError, match="AUDIT_LIMIT_EXCEEDED"):
        await audit.append(event(2))
    assert (await audit.verify()).count == 1


@pytest.mark.anyio
async def test_foreign_key_error_and_cancel_do_not_leak_raw_secret() -> None:
    class BrokenKeys:
        async def key_for(self, key_id: UUID) -> bytes:
            raise RuntimeError("arbitrary-restricted-canary")

    audit = AuditChain(
        chain_id=CHAIN,
        run_id=RUN,
        key_id=KEY,
        policy_fingerprint=event().policy_fingerprint,
        signer=HMACAuditSigner(BrokenKeys()),
        store=MemoryAuditChainStore(),
    )
    with pytest.raises(SecurityPolicyError) as error:
        await audit.append(event())
    assert "arbitrary-restricted-canary" not in "".join(
        traceback.format_exception(error.value)
    )
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "field", ["actor_id", "provider_id", "model_id", "prompt_id", "event_id"]
)
@pytest.mark.anyio
async def test_arbitrary_text_cannot_be_hidden_in_identity(field: str) -> None:
    with pytest.raises(SecurityPolicyError, match="AUDIT_EVENT_INVALID") as error:
        await chain().append(event().model_copy(update={field: "restricted-canary"}))
    assert "restricted-canary" not in str(error.value)
