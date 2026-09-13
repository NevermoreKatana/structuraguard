"""Проекция dry-run связывает именно нормализованные значения с MappingPlan."""

import pytest
from tests.fakes.mapping_validation import case
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import IntegerScalar
from structuraguard.contracts.loading import DryRunRequest
from structuraguard.exceptions import LoadError
from structuraguard.loading.projection import prepare

pytestmark = pytest.mark.anyio


async def request() -> DryRunRequest:
    plan, _, _, _, _ = await case()
    batches = tuple(
        [
            b
            async for b in normalized_stream(
                [{"id": IntegerScalar(value=1), "amount": IntegerScalar(value=12)}],
                semantic_type="integer",
            )
        ]
    )
    return DryRunRequest(batches=batches, mapping=plan)


async def test_projection_uses_exact_values_and_stable_lineage() -> None:
    incoming = await request()
    prepared = await prepare(incoming)
    row = prepared.data.records[0]
    assert row.collection_id == "public.orders"
    assert {c.field_id: c.value.value for c in row.values} == {"id": 1, "amount": 12}
    assert prepared.origins[row.record_id].record_id == "a_r_0"
    assert prepared.origins[row.record_id].value_ids == ("a_v_0_0", "a_v_0_1")
    assert (await prepare(incoming)).data == prepared.data


async def test_incomplete_stream_rejected_before_io() -> None:
    incoming = await request()
    incoming = incoming.model_copy(update={"batches": incoming.batches[:-1]})
    with pytest.raises(LoadError, match="DRY_RUN_INPUT_INVALID"):
        await prepare(incoming)


async def test_forged_mapping_hash_rejected() -> None:
    incoming = await request()
    incoming = incoming.model_copy(
        update={
            "mapping": incoming.mapping.model_copy(
                update={"database_fingerprint": "sha256:" + "a" * 64}
            )
        }
    )
    with pytest.raises(LoadError, match="DRY_RUN_INPUT_INVALID"):
        await prepare(incoming)
