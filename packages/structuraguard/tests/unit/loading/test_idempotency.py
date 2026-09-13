"""Binding не зависит от транспортного batch size и не содержит исходный key."""

import pytest
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping_validation import case, replan

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import LoadLedgerPolicy, PostgreSQLLoadPolicy
from structuraguard.exceptions import LoadError
from structuraguard.loading.idempotency import binding_fingerprint, identity

pytestmark = pytest.mark.anyio


async def test_idempotency_binding_changes_with_source_plan_and_effective_policy() -> (
    None
):
    _, _, catalog, _, _ = await case()
    request, preflight = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=2), "amount": IntegerScalar(value=20)}],
        table_name="orders",
    )
    policy = PostgreSQLLoadPolicy(
        preflight=preflight,
        write_tables=(("public", "orders"),),
        ledger=LoadLedgerPolicy(namespace="tenant"),
    )
    original = binding_fingerprint(request, policy)
    assert (
        binding_fingerprint(request, policy.model_copy(update={"batch_size": 1}))
        == original
    )
    changed = request.model_copy(
        update={"mapping": replan(request.mapping, plan_id="other")}
    )
    assert binding_fingerprint(changed, policy) != original
    changed_policy = policy.model_copy(
        update={
            "preflight": preflight.model_copy(
                update={"error_policy": "quarantine_invalid"}
            )
        }
    )
    assert binding_fingerprint(request, changed_policy) != original
    assert policy.ledger is not None
    scope, key = identity(policy.ledger, "target", "secret-idempotency-key-55")
    assert "secret" not in scope + key
    assert identity(policy.ledger, "target", "different") != (scope, key)
    other, _ = await dry_run_case(
        catalog,
        [{"id": IntegerScalar(value=3), "amount": IntegerScalar(value=30)}],
        table_name="orders",
    )
    stale = request.model_copy(update={"batches": other.batches})
    with pytest.raises(LoadError, match="LOAD_INPUT_BINDING_MISMATCH"):
        binding_fingerprint(stale, policy)
    assert identity(policy.ledger, "other-target", "secret-idempotency-key-55") != (
        scope,
        key,
    )
