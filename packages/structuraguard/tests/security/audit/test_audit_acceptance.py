"""M14 D2/D3: stage evidence, bounded history и общий store contract."""

import asyncio
import traceback
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from tests.contract_suites.audit import (
    assert_audit_store_contract,
    audit_chain,
    audit_event,
)

from structuraguard.contracts.audit import (
    AuditEnvelope,
    AuditKind,
    AuditValidationSummary,
    SecurityAuditEvent,
)
from structuraguard.contracts.common import DataClassification
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security.audit_store import MemoryAuditChainStore


@pytest.mark.anyio
async def test_memory_audit_store_contract() -> None:
    await assert_audit_store_contract(MemoryAuditChainStore())


def completed_event(kind: AuditKind) -> SecurityAuditEvent:
    values = audit_event().model_dump()
    values.update(kind=kind, status="completed", decision="allowed")
    if kind is AuditKind.RUN_STARTED:
        return audit_event()
    if kind is AuditKind.RUN_FAILED:
        values.update(status="failed", decision="error")
    if kind is AuditKind.LLM_FINISHED:
        values.update(
            provider_id=UUID(int=5),
            model_id=UUID(int=6),
            prompt_id=UUID(int=7),
            classification=DataClassification.RESTRICTED,
        )
    if kind is AuditKind.LOAD_COMMITTED:
        values.update(
            source_fingerprint="sha256:" + "b" * 64,
            database_fingerprint="sha256:" + "c" * 64,
            mapping_fingerprint="sha256:" + "d" * 64,
            target_id=UUID(int=8),
            validation=AuditValidationSummary(checked=1, accepted=1, rejected=0),
            inserted=1,
        )
    return SecurityAuditEvent.model_validate(values)


@pytest.mark.anyio
@pytest.mark.parametrize("kind", list(AuditKind))
async def test_each_audit_stage_round_trips_and_authenticates(kind: AuditKind) -> None:
    audit = audit_chain(MemoryAuditChainStore())
    record = await audit.append(completed_event(kind))
    restored = AuditEnvelope.model_validate_json(record.canonical_json())
    assert restored == record
    assert (await audit.verify(expected_head=restored.head)).anchored


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        (AuditKind.RUN_STARTED, "source_fingerprint", "sha256:" + "b" * 64),
        (AuditKind.RUN_STARTED, "decision", "allowed"),
        (AuditKind.SECURITY_DECISION, "decision", "blocked"),
        (AuditKind.PARSER_FINISHED, "inserted", 1),
        (AuditKind.VALIDATION_FINISHED, "updated", 1),
        (AuditKind.RUN_FAILED, "status", "completed"),
        *(
            (AuditKind.LLM_FINISHED, name, None)
            for name in ("provider_id", "model_id", "prompt_id", "classification")
        ),
        *(
            (AuditKind.LOAD_COMMITTED, name, None)
            for name in (
                "source_fingerprint",
                "database_fingerprint",
                "mapping_fingerprint",
                "target_id",
                "validation",
            )
        ),
        (AuditKind.LOAD_COMMITTED, "inserted", -1),
        (AuditKind.LOAD_COMMITTED, "updated", 2**63),
        (
            AuditKind.LOAD_COMMITTED,
            "validation",
            AuditValidationSummary.model_construct(checked=1, accepted=2, rejected=0),
        ),
    ],
)
async def test_invalid_stage_evidence_never_reaches_store(
    kind: AuditKind, field: str, value: object
) -> None:
    class UnreachableStore(MemoryAuditChainStore):
        async def find(self, chain_id: UUID, event_id: UUID) -> AuditEnvelope | None:
            raise AssertionError("invalid evidence reached store")

    with pytest.raises(SecurityPolicyError, match="AUDIT_EVENT_INVALID"):
        await audit_chain(UnreachableStore()).append(
            completed_event(kind).model_copy(update={field: value})
        )


@pytest.mark.anyio
async def test_canonical_timestamp_offset_does_not_change_signature() -> None:
    utc = audit_event()
    local = utc.model_copy(
        update={
            "occurred_at": datetime(2026, 9, 13, 3, tzinfo=timezone(timedelta(hours=3)))
        }
    )
    first = await audit_chain(MemoryAuditChainStore()).append(utc)
    second = await audit_chain(MemoryAuditChainStore()).append(local)
    assert first.canonical_json() == second.canonical_json()
    assert first.current_hash == second.current_hash


@pytest.mark.anyio
async def test_verify_pages_history_and_anchor_prefix_at_256_boundary() -> None:
    class PageStore(MemoryAuditChainStore):
        def __init__(self) -> None:
            super().__init__()
            self.reads: list[tuple[int, int]] = []

        async def read(
            self, chain_id: UUID, *, after: int, limit: int
        ) -> tuple[AuditEnvelope, ...]:
            self.reads.append((after, limit))
            return await super().read(chain_id, after=after, limit=limit)

    store = PageStore()
    audit = audit_chain(store)
    records = [await audit.append(audit_event(i)) for i in range(257)]
    assert (await audit.verify()).count == 257
    assert store.reads == [(0, 256), (256, 256), (257, 256)]
    store.reads.clear()
    assert (await audit.verify_through(records[255].head)).count == 256
    assert store.reads == [(0, 256)]
    assert (await audit.verify_through(records[256].head)).count == 257


@pytest.mark.anyio
async def test_verification_limit_stops_lazy_input_after_one_excess_record() -> None:
    original = audit_chain(MemoryAuditChainStore())
    first = await original.append(audit_event())
    second = await original.append(audit_event(2))

    def records() -> Iterator[AuditEnvelope]:
        yield first
        yield second
        raise AssertionError("verification consumed beyond first excess record")

    with pytest.raises(SecurityPolicyError, match="AUDIT_LIMIT_EXCEEDED"):
        await audit_chain(MemoryAuditChainStore(), max_events=1).verify_records(
            records()
        )


@pytest.mark.anyio
async def test_cancelled_store_propagates_without_raw_diagnostics() -> None:
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    class PendingStore(MemoryAuditChainStore):
        async def find(self, chain_id: UUID, event_id: UUID) -> AuditEnvelope | None:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
            return None

    task = asyncio.create_task(audit_chain(PendingStore()).append(audit_event()))
    await entered.wait()
    task.cancel("restricted-cancellation-canary")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert cleaned.is_set()
    assert not caught.value.args
    assert "restricted-cancellation-canary" not in "".join(
        traceback.format_exception(caught.value)
    )
