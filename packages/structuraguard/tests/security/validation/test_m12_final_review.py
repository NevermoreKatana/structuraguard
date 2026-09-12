"""Регрессии final review: inert DTO storage и ранний отказ неверного timestamp."""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Never, cast

import pytest
from tests.fakes.provenance import provenance_fixture

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.json_schema import JsonSchemaPolicy, JsonSchemaResource
from structuraguard.contracts.normalization import NormalizerSpec
from structuraguard.exceptions import ValidationError
from structuraguard.normalization import NormalizerRegistry
from structuraguard.structure import ParsePlanExecutor
from structuraguard.validation import JsonSchemaValidator, ProvenanceValidator


def _replace_key(value: FrozenContract, name: str, calls: list[str]) -> None:
    class ForeignKey:
        def __hash__(self) -> int:
            calls.append("hash")
            return hash(name)

        def __eq__(self, other: object) -> bool:
            calls.append("equal")
            return other == name

    storage = cast(dict[object, object], vars(value))
    original = storage.pop(name)
    storage[ForeignKey()] = original
    calls.clear()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "boundary", ["normalization", "provenance", "schema_policy", "schema_resource"]
)
async def test_foreign_dto_keys_never_execute_hooks(boundary: str) -> None:
    calls: list[str] = []
    if boundary == "normalization":
        value = StringScalar(value="restricted-input")
        _replace_key(value, "value", calls)
        with pytest.raises(ValidationError) as error:
            NormalizerRegistry.with_builtins().freeze().normalize(
                value, steps=(NormalizerSpec(normalizer_id="trim"),)
            )
        assert error.value.error_code == "NORMALIZATION_INPUT_INVALID"
    elif boundary == "provenance":
        fixture = await provenance_fixture()
        raw = fixture.normalized[0].records[0].entities[0].values[0].raw_value
        _replace_key(raw, "value", calls)
        with pytest.raises(ValidationError) as error:
            await ProvenanceValidator().validate(
                fixture.normalized,
                source_batches=fixture.physical,
                plan=fixture.plan,
                context=fixture.context,
                generated_at=datetime(2026, 9, 13, tzinfo=UTC),
            )
        assert error.value.error_code == "PROVENANCE_INPUT_INVALID"
    elif boundary == "schema_policy":
        policy = JsonSchemaPolicy()
        _replace_key(policy, "max_nodes", calls)
        with pytest.raises(ValidationError) as error:
            JsonSchemaValidator(policy=policy)
        assert error.value.error_code == "JSON_SCHEMA_POLICY_INVALID"
    else:
        uri = "urn:structuraguard:schema:review"
        resource = JsonSchemaResource(uri=uri, document_json="{}")
        _replace_key(resource, "document_json", calls)
        with pytest.raises(ValidationError) as error:
            JsonSchemaValidator(
                policy=JsonSchemaPolicy(allowed_resource_uris=(uri,)),
                resources=(resource,),
            )
        assert error.value.error_code == "JSON_SCHEMA_RESOURCE_INVALID"
    assert calls == []


@pytest.mark.parametrize("boundary", ["schema_policy", "schema_resource"])
def test_schema_storage_subclass_is_rejected_before_iteration(boundary: str) -> None:
    calls: list[str] = []

    class ForeignStorage(dict[str, object]):
        def __iter__(self) -> Never:
            calls.append("iteration")
            raise AssertionError("Foreign storage executed")

    uri = "urn:structuraguard:schema:review"
    policy = JsonSchemaPolicy(allowed_resource_uris=(uri,))
    resource = JsonSchemaResource(uri=uri, document_json="{}")
    value = policy if boundary == "schema_policy" else resource
    object.__setattr__(value, "__dict__", ForeignStorage(vars(value)))
    with pytest.raises(ValidationError) as error:
        JsonSchemaValidator(policy=policy, resources=(resource,))
    expected = (
        "JSON_SCHEMA_POLICY_INVALID"
        if boundary == "schema_policy"
        else "JSON_SCHEMA_RESOURCE_INVALID"
    )
    assert error.value.error_code == expected
    assert calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "timestamp",
    [
        "restricted-timestamp",
        0,
        True,
        None,
        date(2026, 9, 13),
        datetime(2026, 9, 13),
        datetime(2026, 9, 13, tzinfo=timezone(timedelta(hours=3))),
    ],
)
async def test_invalid_timestamp_is_rejected_before_replay(
    timestamp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = await provenance_fixture()

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Replay started before timestamp validation")

    monkeypatch.setattr(ParsePlanExecutor, "execute", forbidden)
    with pytest.raises(ValidationError) as error:
        await ProvenanceValidator().validate(
            fixture.normalized,
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=cast(datetime, timestamp),
        )
    assert error.value.error_code == "PROVENANCE_INPUT_INVALID"
    assert "restricted-timestamp" not in str(error.value)
