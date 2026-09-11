"""Недостающие наблюдаемые сценарии критериев приёмки M8."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone

import pytest
from tests.fakes.profiling import make_record, normalized_stream, record_stream

from structuraguard.contracts.common import (
    DateTimeScalar,
    IntegerScalar,
    NormalizedScalar,
    NullScalar,
    StringScalar,
)
from structuraguard.contracts.normalized import NormalizedBatch, SemanticField
from structuraguard.contracts.profiling import (
    ExamplePolicy,
    NormalizedDataProfile,
    NormalizedProfilingOptions,
)
from structuraguard.exceptions import NormalizedProfilingError
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


async def test_empty_iterable_is_not_a_completed_dataset() -> None:
    async def source() -> AsyncIterator[NormalizedBatch]:
        batches: tuple[NormalizedBatch, ...] = ()
        for batch in batches:
            yield batch

    with pytest.raises(NormalizedProfilingError) as caught:
        await NormalizedDataProfiler().profile(source())
    assert caught.value.error_code == "NORMALIZED_PROFILE_INVALID_STREAM"
    assert caught.value.details == {"reason": "missing_terminal"}


async def test_repeated_entities_late_and_schema_only_fields_use_entity_denominator() -> (
    None
):
    record = make_record(
        [
            ("order", {"id": IntegerScalar(value=90)}),
            ("item", {"id": IntegerScalar(value=1)}),
            ("item", {"id": NullScalar(), "late": StringScalar(value="")}),
        ]
    )
    later = make_record([("item", {"late": StringScalar(value="z")})], 1)
    result = await NormalizedDataProfiler().profile(
        record_stream(
            [(record,), (later,)],
            schema_only=(
                SemanticField(
                    entity_type="item", field_name="absent", semantic_type="string"
                ),
            ),
        )
    )
    assert (result.record_count, result.entity_count, result.value_count) == (2, 4, 5)
    assert len(result.fields) == 4
    order, item = result.field("order", "id"), result.field("item", "id")
    assert (order.entity_count, order.non_null_count, order.null_count) == (1, 1, 0)
    assert (item.entity_count, item.present_count, item.missing_count) == (3, 2, 1)
    assert (item.explicit_null_count, item.non_null_count, item.null_count) == (1, 1, 2)
    late, absent = result.field("item", "late"), result.field("item", "absent")
    assert (late.present_count, late.missing_count, late.min_length) == (2, 1, 0)
    assert absent.null_ratio == 1 and absent.unique_ratio is None
    assert absent.entity_count == absent.missing_count == 3


@pytest.mark.parametrize(
    ("values", "name", "strength"),
    [
        (list(range(20)), "id", "candidate"),
        (list(range(19)), "id", "insufficient_evidence"),
        ([1] * 20, "id", "none"),
        ([*range(20), None], "id", "none"),
        (list(range(20)), "quantity", "possible_identifier"),
    ],
)
async def test_integer_identity_requires_names_uniqueness_and_no_nulls(
    values: list[int | None], name: str, strength: str
) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream(
            {name: IntegerScalar(value=v) if v is not None else NullScalar()}
            for v in values
        )
    )
    assert result.fields[0].identity.strength == strength
    assert result.fields[0].identity.kind == "identifier"


@pytest.mark.parametrize("name", ["email", "code"])
async def test_identity_pattern_and_code_hints_do_not_grant_approval(name: str) -> None:
    values = [
        f"person{i}@example.org" if name == "email" else f"X-{i}" for i in range(20)
    ]
    result = await NormalizedDataProfiler().profile(
        normalized_stream({name: StringScalar(value=v)} for v in values)
    )
    field = result.fields[0]
    assert field.identity.strength == "candidate"
    assert field.identity.kind == ("natural_key" if name == "email" else "code")
    assert "approval" not in field.pii.model_dump()


@pytest.mark.parametrize(
    ("values", "categorical", "free_text"),
    [
        (["ready", "pending"] * 10, True, False),
        ([f"category_{i}" for i in range(20)], False, False),
        (["ready"] * 19, False, False),
        (["Свободное описание на русском языке с Unicode 🙂"] * 20, True, True),
        (["short"] * 20, True, False),
    ],
)
async def test_categorical_and_free_text_have_independent_evidence(
    values: list[str], categorical: bool, free_text: bool
) -> None:
    result = await NormalizedDataProfiler().profile(
        normalized_stream({"description": StringScalar(value=v)} for v in values)
    )
    field = result.fields[0]
    assert field.categorical is categorical
    assert ("free_text" in {p.code for p in field.patterns}) is free_text
    assert field.identity.strength == "none"


async def test_seed_and_redaction_change_samples_but_not_full_statistics() -> None:
    rows = [{"quantity": IntegerScalar(value=i)} for i in range(100)]

    async def profile(seed: str, examples: ExamplePolicy) -> NormalizedDataProfile:
        return await NormalizedDataProfiler(
            NormalizedProfilingOptions(seed=seed, examples=examples)
        ).profile(normalized_stream(rows))

    first = await NormalizedDataProfiler(
        NormalizedProfilingOptions(seed="one", examples=ExamplePolicy.LOCAL_RAW)
    ).profile(normalized_stream(rows))
    second = await NormalizedDataProfiler(
        NormalizedProfilingOptions(seed="two", examples=ExamplePolicy.MASKED)
    ).profile(normalized_stream(rows))
    repeated = await profile("one", ExamplePolicy.LOCAL_RAW)
    assert first == repeated
    assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
    a, b = first.fields[0], second.fields[0]
    assert [e.ordinal for e in a.examples] != [e.ordinal for e in b.examples]
    assert all(not e.masked and e.value is not None for e in a.examples)
    assert all(e.masked and e.value is None for e in b.examples)
    assert (a.extrema, a.null_ratio, a.unique_count, a.inference) == (
        b.extrema,
        b.null_ratio,
        b.unique_count,
        b.inference,
    )


async def test_concurrent_calls_on_one_profiler_have_independent_state() -> None:
    profiler = NormalizedDataProfiler()
    first, second = await asyncio.gather(
        profiler.profile(normalized_stream([{"x": IntegerScalar(value=1)}] * 20)),
        profiler.profile(normalized_stream([{"y": StringScalar(value="text")}])),
    )
    assert first.record_count == 20 and second.record_count == 1
    assert [f.field.field_name for f in first.fields] == ["x"]
    assert [f.field.field_name for f in second.fields] == ["y"]
    assert first.normalized_data_fingerprint != second.normalized_data_fingerprint


async def test_valid_link_groups_preserve_content_after_rebatching_and_id_remapping() -> (
    None
):
    async def run(prefix: str, group_size: int) -> str:
        groups = []
        for i in range(2):
            root = make_record(
                [("order", {"id": IntegerScalar(value=i)})], i * 2, prefix=prefix
            )
            child = make_record(
                [
                    ("item", {"id": IntegerScalar(value=10 + i)}),
                    ("detail", {"label": StringScalar(value="x")}),
                ],
                i * 2 + 1,
                prefix=prefix,
            )
            nested = child.entities[1].model_copy(
                update={"parent_entity_id": child.entities[0].entity_id}
            )
            child = child.model_copy(
                update={
                    "parent_record_id": root.record_id,
                    "related_record_ids": (root.record_id,),
                    "entities": (child.entities[0], nested),
                }
            )
            groups.append((root, child))
        batches = [
            b
            async for b in record_stream(
                groups if group_size == 2 else [sum(groups, ())]
            )
        ]
        assert batches[-1].manifest is not None
        batches[-1].manifest.validate_batches(batches)

        async def source() -> AsyncIterator[NormalizedBatch]:
            for batch in batches:
                yield batch

        return (
            await NormalizedDataProfiler().profile(source())
        ).normalized_data_fingerprint

    assert await run("old", 2) == await run("new", 4)


async def test_content_distinguishes_missing_null_empty_names_and_entity_types() -> (
    None
):
    cases: list[tuple[str, dict[str, NormalizedScalar]]] = [
        ("row", {}),
        ("row", {"x": NullScalar()}),
        ("row", {"x": StringScalar(value="")}),
        ("row", {"y": StringScalar(value="")}),
        ("other", {"x": StringScalar(value="")}),
    ]
    fingerprints = [
        (
            await NormalizedDataProfiler().profile(
                normalized_stream(
                    [{"anchor": IntegerScalar(value=1), **row}], entity_type=kind
                )
            )
        ).normalized_data_fingerprint
        for kind, row in cases
    ]
    assert len(set(fingerprints)) == len(cases)


async def test_equivalent_utc_instants_have_equal_content_fingerprints() -> None:
    utc = datetime(2026, 1, 2, 10, tzinfo=UTC)
    moscow = datetime(2026, 1, 2, 13, tzinfo=timezone(timedelta(hours=3)))
    results = [
        await NormalizedDataProfiler().profile(
            normalized_stream([{"at": DateTimeScalar(value=value)}])
        )
        for value in (utc, moscow)
    ]
    assert (
        results[0].normalized_data_fingerprint == results[1].normalized_data_fingerprint
    )
    assert results[0].fields[0].extrema == results[1].fields[0].extrema


@pytest.mark.parametrize("link", ["parent_entity", "parent_record", "related_record"])
async def test_topology_mutation_changes_validated_complete_content_hash(
    link: str,
) -> None:
    first = make_record(
        [
            ("order", {"id": IntegerScalar(value=1)}),
            ("item", {"id": IntegerScalar(value=2)}),
        ]
    )
    second = make_record([("order", {"id": IntegerScalar(value=3)})], 1)
    baseline = await NormalizedDataProfiler().profile(record_stream([(first, second)]))
    if link == "parent_entity":
        first = first.model_copy(
            update={
                "entities": (
                    first.entities[0],
                    first.entities[1].model_copy(
                        update={"parent_entity_id": first.entities[0].entity_id}
                    ),
                )
            }
        )
    elif link == "parent_record":
        second = second.model_copy(update={"parent_record_id": first.record_id})
    else:
        second = second.model_copy(update={"related_record_ids": (first.record_id,)})
    changed = await NormalizedDataProfiler().profile(record_stream([(first, second)]))
    assert baseline.normalized_data_fingerprint != changed.normalized_data_fingerprint
    if link == "parent_entity":
        assert len(changed.relationships) == 1
        relationship = changed.relationships[0]
        assert relationship.kind == "parent_child" and relationship.count == 1
        assert (relationship.left.entity_type, relationship.right.entity_type) == (
            "order",
            "item",
        )


async def test_raw_transformation_evidence_is_separate_from_normalized_content() -> (
    None
):
    record = make_record([("row", {"x": IntegerScalar(value=1)})])
    first = await NormalizedDataProfiler().profile(record_stream([(record,)]))
    entity = record.entities[0]
    value = entity.values[0].model_copy(
        update={
            "raw_value": StringScalar(value=" 1 "),
            "transformations": ("trim", "parse_integer"),
        }
    )
    changed = record.model_copy(
        update={"entities": (entity.model_copy(update={"values": (value,)}),)}
    )
    second = await NormalizedDataProfiler().profile(record_stream([(changed,)]))
    assert first.normalized_data_fingerprint == second.normalized_data_fingerprint
    assert (
        first.normalized_manifest_fingerprint != second.normalized_manifest_fingerprint
    )
    assert first.fields == second.fields
