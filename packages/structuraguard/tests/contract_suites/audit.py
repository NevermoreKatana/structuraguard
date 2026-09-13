"""Один наблюдаемый CAS/read контракт для memory и PostgreSQL stores."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from structuraguard.contracts.audit import AuditEnvelope, AuditKind, SecurityAuditEvent
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.audit import AuditChainStore
from structuraguard.security.audit import AuditChain, HMACAuditSigner, MemoryAuditKeys
from structuraguard.security.audit_store import MemoryAuditChainStore


def audit_event(number: int = 1) -> SecurityAuditEvent:
    return SecurityAuditEvent(
        event_id=UUID(int=100 + number),
        run_id=UUID(int=2),
        actor_id=UUID(int=3),
        occurred_at=datetime(2026, 9, 13, tzinfo=UTC),
        kind=AuditKind.RUN_STARTED,
        policy_fingerprint="sha256:" + "a" * 64,
    )


def audit_chain(store: AuditChainStore, *, max_events: int = 10_000) -> AuditChain:
    return AuditChain(
        chain_id=UUID(int=1),
        run_id=UUID(int=2),
        key_id=UUID(int=4),
        policy_fingerprint=audit_event().policy_fingerprint,
        signer=HMACAuditSigner(MemoryAuditKeys({UUID(int=4): b"k" * 32})),
        store=store,
        max_events=max_events,
    )


async def assert_audit_store_contract(store: AuditChainStore) -> AuditEnvelope:
    assert isinstance(store, AuditChainStore)
    chain_id = UUID(int=1)
    other = UUID(int=9)
    assert await store.tail(chain_id) is None
    assert await store.find(chain_id, audit_event().event_id) is None
    assert await store.read(chain_id, after=0, limit=256) == ()
    signed = audit_chain(MemoryAuditChainStore())
    first = await signed.append(audit_event())
    second = await signed.append(audit_event(2))
    assert await store.compare_append(first, None)
    assert not await store.compare_append(second, None)
    assert await store.tail(chain_id) == first
    assert await store.compare_append(second, first.head)
    assert not await store.compare_append(second, first.head)
    duplicate = second.model_copy(
        update={"sequence": 3, "previous_hash": second.current_hash}
    )
    assert not await store.compare_append(duplicate, second.head)
    assert await store.tail(chain_id) == second
    assert await store.find(chain_id, first.event.event_id) == first
    assert await store.find(other, first.event.event_id) is None
    assert await store.tail(other) is None
    assert await store.read(other, after=0, limit=1) == ()
    assert await store.read(chain_id, after=0, limit=1) == (first,)
    assert await store.read(chain_id, after=1, limit=256) == (second,)
    assert await store.read(chain_id, after=2, limit=1) == ()
    for after, limit in ((-1, 1), (True, 1), (0, 0), (0, 257), (0, True)):
        with pytest.raises(SecurityPolicyError, match="AUDIT_POLICY_INVALID"):
            await store.read(chain_id, after=after, limit=limit)
    invalid = second.model_copy(update={"sequence": 4, "event": audit_event(3)})
    with pytest.raises(SecurityPolicyError, match="AUDIT_CHAIN_INVALID"):
        await store.compare_append(invalid, second.head)
    assert await store.read(chain_id, after=0, limit=256) == (first, second)
    return second
