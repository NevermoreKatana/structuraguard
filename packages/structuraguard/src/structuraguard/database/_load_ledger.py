"""Ledger writer использует только connection уже открытой target transaction."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    LoadAuditMetadata,
    LoadLedgerPolicy,
    LoadQuarantineReference,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.loading.idempotency import binding_fingerprint, identity
from structuraguard.loading.projection import failure

from ._load_ledger_schema import check_schema, grants, tables

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


class Ledger:
    """Один key scope на transaction; advisory lock не переживает commit/rollback."""

    def __init__(self, request: LoadRequest, policy: PostgreSQLLoadPolicy) -> None:
        if policy.ledger is None or request.idempotency_key is None:
            raise failure("LOAD_IDEMPOTENCY_REQUIRED")
        self.config: LoadLedgerPolicy = policy.ledger
        self.scope, self.key = identity(
            self.config, request.snapshot.mapping.target_id, request.idempotency_key
        )
        self.binding = binding_fingerprint(request.snapshot, policy)
        self.request, self.policy = request, policy
        self._tables = tables(self.config.schema_name)[1]

    async def claim(self, connection: AsyncConnection) -> PostgreSQLLoadResult | None:
        from sqlalchemy import case, func, select, text

        await check_schema(connection, self.config.schema_name)
        await grants(connection, self.config.schema_name)
        await connection.execute(
            text(
                "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:scope,0))"
            ),
            {"scope": f"loader-v1:{self.scope}:{self.key}"},
        )
        table = self._tables["execution_commits"]
        row = (
            await connection.execute(
                select(
                    func.left(table.c.binding, 72),
                    case(
                        (
                            func.octet_length(table.c.payload)
                            <= self.config.max_receipt_bytes,
                            table.c.payload,
                        ),
                        else_=None,
                    ),
                    func.left(table.c.fingerprint, 72),
                ).where(table.c.scope_hash == self.scope, table.c.key_hash == self.key)
            )
        ).one_or_none()
        if row is None:
            return None
        if row[0] != self.binding:
            raise failure("IDEMPOTENCY_KEY_CONFLICT")
        if (
            not isinstance(row[1], str)
            or len(row[1].encode()) > self.config.max_receipt_bytes
        ):
            raise failure("LOAD_LEDGER_RECEIPT_INVALID")
        result = PostgreSQLLoadResult.model_validate_json(row[1])
        mapping = self.request.snapshot.mapping
        if (
            canonical_sha256_value(result.canonical_json()) != row[2]
            or result.replayed
            or result.attempt_run_id is not None
            or result.target_id != mapping.target_id
            or result.mapping_fingerprint != mapping.fingerprint
            or result.database_fingerprint != mapping.database_fingerprint
            or result.normalized_fingerprint != mapping.normalized_fingerprint
        ):
            raise failure("LOAD_LEDGER_RECEIPT_INVALID")
        return result.model_copy(
            update={
                "replayed": True,
                "attempt_run_id": self.request.staging_context.run_id,
            }
        )

    async def commit(
        self,
        connection: AsyncConnection,
        result: PostgreSQLLoadResult,
        plan: DryRunExecutionPlan,
    ) -> None:
        from sqlalchemy import insert

        payload = result.canonical_json()
        if len(payload.encode()) > self.config.max_receipt_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        base = {"scope_hash": self.scope, "key_hash": self.key, "binding": self.binding}
        await connection.execute(
            insert(self._tables["execution_commits"]),
            {
                **base,
                "payload": payload,
                "fingerprint": canonical_sha256_value(payload),
            },
        )
        run_hash = canonical_sha256_value(result.run_id)
        refs = tuple(
            LoadQuarantineReference(
                run_hash=run_hash,
                record_hash=canonical_sha256_value(s.record_id),
                unit_hash=canonical_sha256_value(s.unit_id),
                table_hash=canonical_sha256_value(s.table_id),
                codes=s.codes,
            )
            for s in plan.steps
            if s.action == "quarantine"
        )
        total = len(payload.encode())
        # Не материализовать неограниченный executemany payload.
        for offset in range(0, len(refs), 100):
            values = []
            for ref in refs[offset : offset + 100]:
                serialized = ref.canonical_json()
                total += len(serialized.encode())
                if total > self.config.max_receipt_bytes:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                values.append(
                    {
                        **base,
                        "unit_hash": ref.unit_hash,
                        "payload": serialized,
                        "fingerprint": canonical_sha256_value(serialized),
                    }
                )
            await connection.execute(
                insert(self._tables["execution_quarantine"]), values
            )
        mapping = self.request.snapshot.mapping
        audit = LoadAuditMetadata(
            mode=self.policy.preflight.error_policy,
            run_hash=run_hash,
            source_fingerprint=mapping.source_fingerprint,
            mapping_fingerprint=mapping.fingerprint,
            normalized_fingerprint=result.normalized_fingerprint,
            database_fingerprint=result.database_fingerprint,
            policy_fingerprint=result.policy_fingerprint,
            binding_fingerprint=self.binding,
            inserted=result.inserted,
            updated=result.updated,
            skipped=result.skipped,
            quarantined=result.quarantined,
            loaded_records=result.loaded_records,
            rejected_records=result.rejected_records,
            generated_at=result.generated_at,
        )
        audit_payload = (result.audit_head or audit).canonical_json()
        await connection.execute(
            insert(self._tables["execution_audit"]),
            {
                **base,
                "payload": audit_payload,
                "fingerprint": canonical_sha256_value(audit_payload),
            },
        )
