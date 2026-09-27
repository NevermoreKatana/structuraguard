"""Native grammar отвергает свойства другого семейства без исправления ответа."""

import json

import pytest
from tests.fakes.semantic import encoded, scenario

from structuraguard.contracts.semantic import LLMStructureSuggestion
from structuraguard.exceptions import LLMProviderError
from structuraguard.parsers.builtin import LogParser
from structuraguard.structure import semantic_response_schema
from structuraguard.structure._semantic_wire import _strip_annotations


@pytest.mark.anyio
@pytest.mark.parametrize(
    "property_name,value",
    [
        ("root_ref", "r0"),
        ("header_row", 0),
        ("data_start_row", 1),
        ("data_end_row", 18),
        ("footer_start_row", 19),
        ("repeated_header_rows", [0]),
    ],
)
async def test_log_native_schema_rejects_tabular_coordinates(
    property_name: str, value: object
) -> None:
    _, _, proposed = await scenario(
        LogParser(),
        b"2026-01-01T10:00:00Z INFO first event\n2026-01-01T10:00:01Z INFO next event\n",
    )
    schema = semantic_response_schema()
    schema.validate(encoded(proposed))
    assert isinstance(proposed["plan"], dict)
    proposed["plan"][property_name] = value
    # Generic public DTO не меняет совместимость; wire schema сужает grammar.
    LLMStructureSuggestion.model_validate_json(encoded(proposed))
    with pytest.raises(LLMProviderError, match="LLM_SCHEMA_VIOLATION"):
        schema.validate(encoded(proposed))
    log_properties = json.loads(schema.schema_json)["$defs"]["_LogPlan"]["properties"]
    if property_name == "repeated_header_rows":
        definitions = json.loads(schema.schema_json)["$defs"]
        target = log_properties[property_name]["$ref"].removeprefix("#/$defs/")
        assert definitions[target]["maxItems"] == 0
    else:
        assert log_properties[property_name]["type"] == "null"


def test_schema_compaction_preserves_property_names_and_literal_objects() -> None:
    schema = {
        "title": "annotations are redundant",
        "description": "also redundant",
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "title": "annotation",
                "const": "literal title",
            },
            "description": {
                "type": "object",
                "const": {"title": "keep", "description": "keep"},
            },
            "choice": {"enum": [{"title": "keep", "description": "keep"}]},
        },
        "required": ["title", "description", "choice"],
        "additionalProperties": False,
    }
    _strip_annotations(schema)
    assert "title" not in schema and "description" not in schema
    assert schema["properties"] == {
        "title": {"type": "string", "const": "literal title"},
        "description": {
            "type": "object",
            "const": {"title": "keep", "description": "keep"},
        },
        "choice": {"enum": [{"title": "keep", "description": "keep"}]},
    }
    assert schema["required"] == ["title", "description", "choice"]
    assert schema["additionalProperties"] is False
