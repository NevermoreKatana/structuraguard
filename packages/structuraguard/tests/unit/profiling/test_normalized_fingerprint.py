"""Canonical frames: тип, порядок, schema и topology вместо runtime IDs."""

from decimal import Decimal

import pytest
from tests.fakes.profiling import AUTO, normalized_stream

from structuraguard.contracts.common import (
    DecimalScalar,
    IntegerScalar,
    NormalizedScalar,
    StringScalar,
)
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
    SemanticField,
    SemanticSourceIndex,
)
from structuraguard.domain.normalized_fingerprint import NormalizedContentHasher
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


async def test_field_order_and_decimal_scale_are_irrelevant() -> None:
    a = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"x": DecimalScalar(value=Decimal("1.00")), "y": StringScalar(value="é")}]
        )
    )
    b = await NormalizedDataProfiler().profile(
        normalized_stream(
            [{"y": StringScalar(value="é"), "x": DecimalScalar(value=Decimal("1.0"))}]
        )
    )
    assert a.normalized_data_fingerprint == b.normalized_data_fingerprint


async def test_scalar_type_and_record_order_change_content_hash() -> None:
    rows: list[NormalizedScalar] = [
        IntegerScalar(value=1),
        StringScalar(value="1"),
        DecimalScalar(value=Decimal(1)),
    ]
    hashes = [
        (
            await NormalizedDataProfiler().profile(normalized_stream([{"x": v}]))
        ).normalized_data_fingerprint
        for v in rows
    ]
    assert len(set(hashes)) == 3
    first = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": v} for v in rows])
    )
    second = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": v} for v in reversed(rows)])
    )
    assert first.normalized_data_fingerprint != second.normalized_data_fingerprint


async def test_manifest_schema_only_field_is_profiled_and_hashed() -> None:
    terminal = await anext(normalized_stream([]))
    assert terminal.manifest is not None
    field = SemanticField(entity_type="unseen", field_name="x", semantic_type="integer")
    manifest = NormalizedDatasetManifest.model_validate(
        terminal.manifest.model_copy(
            update={
                "normalized_fingerprint": AUTO,
                "semantic_schema": (field,),
                "semantic_fields": ("x",),
                "semantic_index": SemanticSourceIndex(fields=(field.ref,)),
            }
        ).model_dump(mode="python")
    )
    changed = NormalizedBatch.model_validate(
        terminal.model_copy(update={"manifest": manifest}).model_dump(mode="python")
    )
    first = await NormalizedDataProfiler().profile(terminal)
    second = await NormalizedDataProfiler().profile(changed)
    assert second.fields[0].entity_count == 0
    assert second.fields[0].null_ratio is None
    assert first.normalized_data_fingerprint != second.normalized_data_fingerprint


async def test_parent_and_related_topology_are_included_in_projection() -> None:
    batches = [
        b
        async for b in normalized_stream(
            [{"x": IntegerScalar(value=1)}, {"x": IntegerScalar(value=2)}]
        )
    ]
    assert batches[-1].manifest is not None
    first = NormalizedContentHasher()
    first.consume(batches[0])
    record_a, record_b = batches[0].records
    linked = record_b.model_copy(
        update={
            "parent_record_id": record_a.record_id,
            "related_record_ids": (record_a.record_id,),
        }
    )
    batch = batches[0].model_copy(update={"records": (record_a, linked)})
    second = NormalizedContentHasher()
    second.consume(batch)
    assert first.finish(batches[-1].manifest) != second.finish(batches[-1].manifest)


async def test_empty_content_has_frozen_v1_golden_vector() -> None:
    result = await NormalizedDataProfiler().profile(normalized_stream([]))
    assert (
        result.normalized_data_fingerprint
        == "sha256:7efd48733e968435f1205c23095c49b8463e15c59ee78a93d1368721fcde9034"
    )
