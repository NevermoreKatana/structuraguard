"""Контракт поддержанного профиля Draft 2020-12 через публичный port."""

import pytest

from structuraguard.ports.json_schema import JsonSchemaValidator as ValidatorPort
from structuraguard.validation import JsonSchemaValidator

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("schema", "instance", "accepted"),
    [
        ({"type": "null"}, None, True),
        ({"type": ["null", "integer"]}, 2, True),
        ({"type": ["null", "integer"]}, "2", False),
        ({"enum": [1]}, True, False),
        ({"const": False}, 0, False),
        ({"minimum": 0, "maximum": 2}, 2, True),
        ({"exclusiveMinimum": 0}, 0, False),
        ({"exclusiveMaximum": 2}, 2, False),
        ({"multipleOf": 0.1}, 0.3, True),
        ({"minLength": 2, "maxLength": 3}, "😀я", True),
        ({"minLength": 2.0}, "я", False),
        ({"minProperties": 1, "maxProperties": 2}, {}, False),
        ({"required": ["x"]}, {"x": None}, True),
        ({"dependentRequired": {"x": ["y"]}}, {"x": 1}, False),
        ({"dependentSchemas": {"x": {"required": ["y"]}}}, {"x": 1}, False),
        ({"propertyNames": {"minLength": 2}}, {"x": 1}, False),
        ({"additionalProperties": {"type": "integer"}}, {"x": "bad"}, False),
        ({"minItems": 1, "maxItems": 2}, [], False),
        ({"uniqueItems": True}, [{"a": 1}, {"a": 1}], False),
        ({"uniqueItems": True}, [True, 1], True),
        ({"prefixItems": [{"const": 1}], "items": False}, [1, 2], False),
        (
            {"contains": {"type": "integer"}, "minContains": 2, "maxContains": 3},
            [1, "x", 2],
            True,
        ),
        ({"contains": {"type": "integer"}, "maxContains": 1}, [1, 2], False),
        ({"prefixItems": [True], "unevaluatedItems": False}, [1, 2], False),
        ({"not": {"type": "string"}}, "text", False),
        ({"oneOf": [{"type": "integer"}, {"type": "number"}]}, 1, False),
        (
            {
                "if": {"type": "integer"},
                "then": {"minimum": 3},
                "else": {"type": "string"},
            },
            2,
            False,
        ),
        (
            {
                "if": {"type": "integer"},
                "then": {"minimum": 3},
                "else": {"type": "string"},
            },
            "ok",
            True,
        ),
        ({"$defs": {"off": False}, "$ref": "#/$defs/off"}, {}, False),
        (
            {
                "$defs": {"positive": {"$anchor": "positive", "minimum": 1}},
                "$ref": "#positive",
            },
            2,
            True,
        ),
    ],
)
async def test_supported_draft_keywords(
    schema: object, instance: object, accepted: bool
) -> None:
    validator: ValidatorPort = JsonSchemaValidator()
    assert isinstance(validator, ValidatorPort)
    result = await validator.validate(instance, schema=schema)
    assert result.schema_valid
    assert result.accepted is accepted
