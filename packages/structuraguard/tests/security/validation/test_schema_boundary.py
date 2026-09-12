"""Минимальные атаки на schema intake, refs, regex и evaluation budget."""

import builtins
import socket
import urllib.request
from decimal import Decimal
from typing import Never

import pytest

from structuraguard.contracts.json_schema import JsonSchemaPolicy, JsonSchemaResource
from structuraguard.exceptions import ValidationError
from structuraguard.validation import JsonSchemaValidator

pytestmark = pytest.mark.anyio


async def test_regex_work_budget_counts_property_name_lengths() -> None:
    validator = JsonSchemaValidator(policy=JsonSchemaPolicy(max_evaluations=100))
    with pytest.raises(ValidationError) as error:
        await validator.validate(
            {"a" * 1000: 1}, schema={"patternProperties": {"^a+$": {"type": "integer"}}}
        )
    assert error.value.error_code == "JSON_SCHEMA_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/missing"},
        {"const": {"hidden": {"pattern": "(a+)+$"}}, "$ref": "#/const/hidden"},
        {"$defs": {"a": {"$anchor": "same"}, "b": {"$anchor": "same"}}},
        {"patternProperties": {"(a+)+$": {}}},
        {"pattern": "[a&&b]"},
        {
            "$vocabulary": {
                "https://json-schema.org/draft/2020-12/vocab/format-assertion": True
            }
        },
    ],
)
async def test_schema_features_cannot_escape_preflight(schema: object) -> None:
    result = await JsonSchemaValidator().validate({}, schema=schema)
    assert not result.schema_valid and result.issues


@pytest.mark.parametrize(
    "document",
    [
        '{"type":"integer","type":"string"}',
        '{"minimum":NaN}',
        '{"minimum":1e9999999999999999999999999}',
    ],
)
async def test_local_resource_json_intake_is_strict_and_bounded(document: str) -> None:
    uri = "urn:structuraguard:schema:numbers"
    with pytest.raises(ValidationError) as error:
        JsonSchemaValidator(
            policy=JsonSchemaPolicy(allowed_resource_uris=(uri,)),
            resources=(JsonSchemaResource(uri=uri, document_json=document),),
        )
    assert error.value.error_code in {
        "JSON_SCHEMA_INPUT_INVALID",
        "JSON_SCHEMA_LIMIT_EXCEEDED",
    }


async def test_report_path_budget_does_not_return_partial_acceptance() -> None:
    validator = JsonSchemaValidator(policy=JsonSchemaPolicy(max_bytes=2048))
    with pytest.raises(ValidationError) as error:
        await validator.validate(
            {}, schema={"required": ["a" * 120, "b" * 120, "c" * 120]}
        )
    assert error.value.error_code == "JSON_SCHEMA_LIMIT_EXCEEDED"


async def test_byte_cache_limit_and_disabled_cache_do_not_skip_validation() -> None:
    for policy in (
        JsonSchemaPolicy(max_cache_bytes=1),
        JsonSchemaPolicy(max_cache_entries=0),
    ):
        validator = JsonSchemaValidator(policy=policy)
        for _ in range(2):
            assert not (await validator.validate(0, schema={"minimum": 1})).accepted
        assert validator.cache_info.entries == 0
        assert validator.cache_info.misses == 2


async def test_local_resource_changes_are_bound_to_schema_fingerprint() -> None:
    uri = "urn:structuraguard:schema:bound"
    policy = JsonSchemaPolicy(allowed_resource_uris=(uri,))
    first = JsonSchemaValidator(
        policy=policy,
        resources=(JsonSchemaResource(uri=uri, document_json='{"minimum":0}'),),
    )
    second = JsonSchemaValidator(
        policy=policy,
        resources=(JsonSchemaResource(uri=uri, document_json='{"minimum":2}'),),
    )
    a = await first.validate(1, schema={"$ref": uri})
    b = await second.validate(1, schema={"$ref": uri})
    assert a.accepted and not b.accepted
    assert a.schema_fingerprint != b.schema_fingerprint


async def test_successful_local_schema_validation_has_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("I/O forbidden")

    validator = JsonSchemaValidator()
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    assert (
        await validator.validate(
            {"age": 1},
            schema={
                "$defs": {"positive": {"minimum": 0}},
                "properties": {"age": {"$ref": "#/$defs/positive"}},
            },
        )
    ).accepted


async def test_forged_policy_is_rejected_before_serializer_hooks() -> None:
    class Trap:
        def __repr__(self) -> Never:
            raise AssertionError("Serializer reached untrusted object")

    policy = JsonSchemaPolicy.model_construct(max_nodes=Trap())
    with pytest.raises(ValidationError) as error:
        JsonSchemaValidator(policy=policy)
    assert error.value.error_code == "JSON_SCHEMA_POLICY_INVALID"


async def test_safe_patterns_and_property_names_do_not_accept_unicode_digit_lookalikes() -> (
    None
):
    validator = JsonSchemaValidator()
    assert (
        await validator.validate("ABC12", schema={"pattern": "^[A-Z0-9]{1,8}$"})
    ).accepted
    assert not (
        await validator.validate("１２", schema={"pattern": "^[0-9]+$"})
    ).accepted
    assert (
        await validator.validate(
            {"x_1": 2},
            schema={
                "patternProperties": {"^x_[0-9]+$": {"type": "integer"}},
                "additionalProperties": False,
            },
        )
    ).accepted
    assert not (
        await validator.validate(
            {"x_１": 2},
            schema={
                "patternProperties": {"^x_[0-9]+$": {}},
                "additionalProperties": False,
            },
        )
    ).accepted


async def test_type_tagged_schema_fingerprint_separates_decimal_and_text() -> None:
    validator = JsonSchemaValidator()
    numeric = await validator.validate(Decimal("0.1"), schema={"const": Decimal("0.1")})
    textual = await validator.validate(Decimal("0.1"), schema={"const": "0.1"})
    assert numeric.accepted and not textual.accepted
    assert numeric.schema_fingerprint != textual.schema_fingerprint


@pytest.mark.parametrize(
    "ref",
    [
        "https://example.invalid/private",
        "http://169.254.169.254/latest",
        "file:///etc/passwd",
        "//example.invalid/x",
        "../schema.json",
        "data:application/json,{}",
    ],
)
async def test_remote_refs_are_rejected_without_io(
    ref: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("I/O forbidden")

    validator = JsonSchemaValidator()
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(builtins, "open", forbidden)
    result = await validator.validate({}, schema={"$defs": {"unused": {"$ref": ref}}})
    assert not result.schema_valid
    assert {i.code for i in result.issues} == {"JSON_SCHEMA_REF_FORBIDDEN"}


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#"},
        {"allOf": [{"$ref": "#"}]},
        {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}},
    ],
)
async def test_nonprogress_recursive_schemas_are_rejected(
    schema: dict[str, object],
) -> None:
    result = await JsonSchemaValidator().validate({}, schema=schema)
    assert not result.schema_valid
    assert "JSON_SCHEMA_REF_CYCLE" in {i.code for i in result.issues}


@pytest.mark.parametrize(
    "pattern",
    ["(a+)+$", "^(a|aa)+$", r"^(a)\1$", "(?=a)", "^a*a*$", "a+$", "^a{1,10000000}$"],
)
async def test_unsafe_regex_is_refused_before_matching(pattern: str) -> None:
    result = await JsonSchemaValidator().validate(
        "a" * 100 + "!", schema={"pattern": pattern}
    )
    assert not result.schema_valid
    assert {i.code for i in result.issues} == {"JSON_SCHEMA_FEATURE_UNSUPPORTED"}


async def test_oversized_deep_cyclic_and_nonfinite_inputs_fail_closed() -> None:
    validator = JsonSchemaValidator(
        policy=JsonSchemaPolicy(max_depth=4, max_properties=2)
    )
    schemas: list[object] = [{"properties": {"a": {}, "b": {}, "c": {}}}]
    deep: dict[str, object] = {}
    for _ in range(5):
        deep = {"not": deep}
    schemas.append(deep)
    for schema in schemas:
        with pytest.raises(ValidationError) as error:
            await validator.validate({}, schema=schema)
        assert error.value.error_code == "JSON_SCHEMA_LIMIT_EXCEEDED"
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValidationError):
        await validator.validate(cyclic, schema=True)
    with pytest.raises(ValidationError):
        await validator.validate(float("nan"), schema=True)


async def test_nested_draft_switch_and_dynamic_refs_cannot_bypass_controls() -> None:
    for schema in (
        {"$defs": {"x": {"$schema": "http://json-schema.org/draft-07/schema#"}}},
        {"$dynamicRef": "#node"},
        {"$defs": {"x": {"$id": "https://example.invalid"}}},
    ):
        assert not (
            await JsonSchemaValidator().validate({}, schema=schema)
        ).schema_valid


async def test_work_limit_is_enforced_even_with_same_draft_declared_in_subschema() -> (
    None
):
    validator = JsonSchemaValidator(policy=JsonSchemaPolicy(max_evaluations=10))
    schema = {
        "type": "array",
        "items": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "integer",
        },
    }
    with pytest.raises(ValidationError) as error:
        await validator.validate(list(range(30)), schema=schema)
    assert error.value.error_code == "JSON_SCHEMA_LIMIT_EXCEEDED"
