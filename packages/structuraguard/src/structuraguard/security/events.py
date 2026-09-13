"""Closed audit projection: исходные values/SQL/exception text не копируются."""

from datetime import datetime
from uuid import UUID, uuid5

from structuraguard.contracts.audit import (
    AuditKind,
    AuditValidationSummary,
    SecurityAuditEvent,
)
from structuraguard.contracts.database_policy import PostgreSQLAuditPolicy
from structuraguard.contracts.loading import LoadRequest, PostgreSQLLoadResult


def load_chain_identity(
    policy: PostgreSQLAuditPolicy, run_id: str
) -> tuple[UUID, UUID]:
    """Связать legacy opaque staging run с UUID namespace trusted owner."""
    return uuid5(policy.namespace, "chain:" + run_id), uuid5(
        policy.namespace, "run:" + run_id
    )


def committed_load_event(
    policy: PostgreSQLAuditPolicy, request: LoadRequest, result: PostgreSQLLoadResult
) -> SecurityAuditEvent:
    chain, run = load_chain_identity(policy, result.run_id)
    return SecurityAuditEvent(
        event_id=uuid5(chain, "load_committed"),
        run_id=run,
        actor_id=policy.actor_id,
        occurred_at=result.generated_at,
        kind=AuditKind.LOAD_COMMITTED,
        status="completed",
        decision="allowed",
        policy_fingerprint=result.policy_fingerprint,
        source_fingerprint=request.snapshot.mapping.source_fingerprint,
        database_fingerprint=result.database_fingerprint,
        mapping_fingerprint=result.mapping_fingerprint,
        target_id=uuid5(policy.namespace, "target:" + result.target_id),
        validation=AuditValidationSummary(
            checked=result.loaded_records + result.rejected_records,
            accepted=result.loaded_records,
            rejected=result.rejected_records,
        ),
        inserted=result.inserted,
        updated=result.updated,
    )


def failed_load_event(
    policy: PostgreSQLAuditPolicy,
    request: LoadRequest,
    *,
    occurred_at: datetime,
    policy_fingerprint: str,
    cancelled: bool,
) -> SecurityAuditEvent:
    """Событие после подтверждённого rollback; текст первичной ошибки исключён."""
    chain, run = load_chain_identity(policy, request.staging_context.run_id)
    return SecurityAuditEvent(
        event_id=uuid5(chain, "load_rolled_back"),
        run_id=run,
        actor_id=policy.actor_id,
        occurred_at=occurred_at,
        kind=AuditKind.RUN_FAILED,
        status="cancelled" if cancelled else "failed",
        decision="error",
        policy_fingerprint=policy_fingerprint,
        source_fingerprint=request.snapshot.mapping.source_fingerprint,
        database_fingerprint=request.snapshot.mapping.database_fingerprint,
        mapping_fingerprint=request.snapshot.mapping.fingerprint,
        target_id=uuid5(
            policy.namespace, "target:" + request.snapshot.mapping.target_id
        ),
    )
