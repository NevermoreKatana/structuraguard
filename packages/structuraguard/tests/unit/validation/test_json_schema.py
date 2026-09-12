"""Наблюдаемый контракт локального Draft 2020-12 validator."""

from decimal import Decimal, localcontext

import pytest

from structuraguard.contracts.json_schema import JsonSchemaPolicy, JsonSchemaResource
from structuraguard.exceptions import ValidationError
from structuraguard.validation import JsonSchemaValidator

pytestmark = pytest.mark.anyio


async def test_collects_independent_errors_with_paths_without_changing_input() -> None:
    schema = {
        "type": "object",
        "required": ["id", "name"],
        "properties": {
            "age": {"type": "integer", "minimum": 18},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": False,
    }
    instance = {"age": 10, "tags": [1, False], "unexpected": "private"}
    validator = JsonSchemaValidator()
    result = await validator.validate(instance, schema=schema)
    assert result.schema_valid and not result.accepted
    assert {(issue.json_pointer, issue.code) for issue in result.issues} == {
        ("/id", "JSON_SCHEMA_REQUIRED"),
        ("/name", "JSON_SCHEMA_REQUIRED"),
        ("/age", "JSON_SCHEMA_MINIMUM"),
        ("/tags/0", "JSON_SCHEMA_TYPE"),
        ("/tags/1", "JSON_SCHEMA_TYPE"),
        ("/unexpected", "JSON_SCHEMA_ADDITIONAL_PROPERTIES"),
    }
    assert instance == {"age": 10, "tags": [1, False], "unexpected": "private"}
    assert "private" not in repr(result)
    assert result == await validator.validate(instance, schema=schema)


async def test_meta_validation_collects_malformed_keywords_before_instance() -> None:
    result = await JsonSchemaValidator().validate(
        {}, schema={"type": "unknown", "required": "id", "minimum": "zero"}
    )
    assert not result.schema_valid and not result.accepted
    assert {issue.phase for issue in result.issues} == {"schema"}
    assert {issue.instance_path[0] for issue in result.issues} == {
        "type",
        "required",
        "minimum",
    }
    assert all(issue.code == "JSON_SCHEMA_INVALID_SCHEMA" for issue in result.issues)


async def test_local_ref_with_siblings_and_guarded_recursive_tree() -> None:
    schema = {
        "$defs": {"positive": {"type": "integer", "minimum": 1}},
        "type": "object",
        "properties": {
            "value": {"$ref": "#/$defs/positive", "maximum": 5},
            "children": {"type": "array", "items": {"$ref": "#"}},
        },
        "additionalProperties": False,
    }
    validator = JsonSchemaValidator()
    assert (
        await validator.validate(
            {"value": 2, "children": [{"value": 4}]}, schema=schema
        )
    ).accepted
    result = await validator.validate(
        {"value": 8, "children": [{"value": -1}]}, schema=schema
    )
    assert {i.json_pointer for i in result.issues} == {"/value", "/children/0/value"}


async def test_approved_local_resource_is_copied_and_resolved_without_retrieval() -> (
    None
):
    uri = "urn:structuraguard:schema:amount"
    validator = JsonSchemaValidator(
        policy=JsonSchemaPolicy(allowed_resource_uris=(uri,)),
        resources=(
            JsonSchemaResource(uri=uri, document_json='{"type":"number","minimum":0}'),
        ),
    )
    assert (await validator.validate(2, schema={"$ref": uri})).accepted
    assert not (await validator.validate(-1, schema={"$ref": uri})).accepted
    assert not (
        await JsonSchemaValidator().validate(2, schema={"$ref": uri})
    ).schema_valid


async def test_decimal_multiple_of_is_exact_and_context_independent() -> None:
    validator = JsonSchemaValidator()
    with localcontext() as context:
        context.prec = 2
        assert (
            await validator.validate(
                Decimal("123456.3"),
                schema={"type": "number", "multipleOf": Decimal("0.1")},
            )
        ).accepted
        assert not (
            await validator.validate(
                Decimal("0.31"), schema={"multipleOf": Decimal("0.1")}
            )
        ).accepted
        assert (
            await validator.validate(Decimal("2.0"), schema={"type": "integer"})
        ).accepted
        assert not (await validator.validate(True, schema={"type": "integer"})).accepted


async def test_draft_compositions_arrays_and_unevaluated_properties() -> None:
    validator = JsonSchemaValidator()
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "allOf": [{"properties": {"id": {"type": "integer"}}}],
        "unevaluatedProperties": False,
    }
    assert (await validator.validate({"id": 1}, schema=schema)).accepted
    assert not (await validator.validate({"id": 1, "extra": 2}, schema=schema)).accepted
    assert (
        await validator.validate(
            ["id", 1, 2],
            schema={
                "prefixItems": [{"type": "string"}],
                "items": {"type": "integer"},
                "contains": {"const": 2},
            },
        )
    ).accepted
    result = await validator.validate(
        False, schema={"anyOf": [{"type": "integer"}, {"type": "string"}]}
    )
    assert {i.code for i in result.issues} == {"JSON_SCHEMA_ANY_OF", "JSON_SCHEMA_TYPE"}


async def test_format_is_annotation_defaults_are_not_repairs_and_boolean_schemas() -> (
    None
):
    validator = JsonSchemaValidator()
    assert (
        await validator.validate("invalid email", schema={"format": "email"})
    ).accepted
    original: dict[str, str] = {}
    assert (
        await validator.validate(
            original, schema={"properties": {"a": {"default": "b"}}}
        )
    ).accepted
    assert original == {}
    assert (await validator.validate(None, schema=True)).accepted
    assert not (await validator.validate(None, schema=False)).accepted


async def test_cache_is_bounded_per_instance_and_does_not_trust_mutated_schema() -> (
    None
):
    validator = JsonSchemaValidator(policy=JsonSchemaPolicy(max_cache_entries=2))
    for minimum in range(4):
        await validator.validate(10, schema={"minimum": minimum})
    assert validator.cache_info.entries == 2
    assert validator.cache_info.evictions == 2
    assert JsonSchemaValidator().cache_info.entries == 0
    schema = {"minimum": 0}
    assert (await validator.validate(1, schema=schema)).accepted
    schema["minimum"] = 3
    assert not (await validator.validate(1, schema=schema)).accepted
    hits = validator.cache_info.hits
    await validator.validate(1, schema=schema)
    assert validator.cache_info.hits == hits + 1


async def test_issue_budget_raises_without_partial_report() -> None:
    with pytest.raises(ValidationError) as error:
        await JsonSchemaValidator(policy=JsonSchemaPolicy(max_issues=1)).validate(
            {}, schema={"required": ["a", "b"]}
        )
    assert error.value.error_code == "JSON_SCHEMA_LIMIT_EXCEEDED"
