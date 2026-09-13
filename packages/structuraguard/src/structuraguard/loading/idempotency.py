"""Чистое связывание idempotency scope с содержимым и семантикой исполнения."""

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.loading import (
    DryRunRequest,
    LoadLedgerPolicy,
    PostgreSQLLoadPolicy,
)

from .projection import failure


def identity(policy: LoadLedgerPolicy, target_id: str, key: str) -> tuple[str, str]:
    return (
        canonical_sha256_value(("loader-scope-v1", policy.namespace, target_id)),
        canonical_sha256_value(("loader-key-v1", key)),
    )


def binding_fingerprint(request: DryRunRequest, policy: PostgreSQLLoadPolicy) -> str:
    mapping = request.mapping
    manifest = request.batches[-1].manifest
    if manifest is None or (
        manifest.source.source_fingerprint,
        manifest.extraction_fingerprint,
        manifest.parse_plan_fingerprint,
        manifest.normalized_fingerprint,
    ) != (
        mapping.source_fingerprint,
        mapping.extraction_fingerprint,
        mapping.parse_plan_fingerprint,
        mapping.normalized_fingerprint,
    ):
        raise failure("LOAD_INPUT_BINDING_MISMATCH")
    effective = policy.model_dump(
        exclude={"batch_size", "max_parameters", "max_batch_bytes", "ledger"}
    )
    return canonical_sha256_value(
        (
            "postgresql-loader-v2",
            mapping.source_fingerprint,
            mapping.extraction_fingerprint,
            mapping.parse_plan_fingerprint,
            mapping.normalized_fingerprint,
            mapping.fingerprint,
            mapping.database_fingerprint,
            mapping.target_id,
            mapping.target_policy_fingerprint,
            effective,
        )
    )
