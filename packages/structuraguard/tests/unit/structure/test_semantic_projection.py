"""Source-bound LOG projection фиксирует технику, но оставляет семантику модели."""

import copy
import json

import pytest

from structuraguard.contracts.semantic import LLMStructureSuggestion
from structuraguard.exceptions import LLMProviderError
from structuraguard.structure import semantic_response_schema
from structuraguard.structure._semantic_projection import project_semantic_schema


def payload() -> dict[str, object]:
    return {
        "source_catalog": [
            {"ref": "r0", "kind": "line"},
            {"ref": "r1", "kind": "line"},
        ],
        "log_json_shapes": [
            {
                "refs": ["r0", "r1"],
                "selector": "log_json",
                "offset": 0,
                "scalar_paths": [["requestID"], ["context", "status"]],
            }
        ],
        "parsing_policy": {"max_fields": 64},
    }


def test_projection_binds_all_paths_records_and_technical_ids() -> None:
    schema = semantic_response_schema()
    base = json.loads(schema.schema_json)
    original = copy.deepcopy(base)
    result = json.loads(
        json.dumps(project_semantic_schema(base, json.dumps(payload())))
    )
    assert base == original
    assert "$defs" not in result
    assert len(json.dumps(result)) < 65536
    props = result["properties"]["plan"]["anyOf"][0]["properties"]
    assert props["scope"] == {"const": ["r0", "r1"]}
    assert props["fields"]["minItems"] == props["fields"]["maxItems"] == 2
    fields = props["fields"]["prefixItems"]
    assert [field["properties"]["field_id"] for field in fields] == [
        {"const": "f0"},
        {"const": "f1"},
    ]
    assert fields[1]["properties"]["selector"]["const"]["path"] == [
        {"operation": "key", "name": "context", "occurrence": 0},
        {"operation": "key", "name": "status", "occurrence": 0},
    ]
    for field in fields:
        assert list(field["properties"]) == [
            "field_id",
            "selector",
            "semantic_name",
            "semantic_type",
            "locale_hint",
            "source_refs",
        ]
        assert "const" not in field["properties"]["semantic_name"]
        assert "const" not in field["properties"]["semantic_type"]
    entity = props["entities"]["prefixItems"][0]["properties"]
    assert entity["field_ids"] == {"const": ["f0", "f1"]}
    assert entity["records"] == {"const": [["r0"], ["r1"]]}
    assert "const" not in entity["entity_type"]


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "partial",
        "mixed",
        "duplicate_paths",
        "too_many",
        "two_shapes",
        "invalid_offset",
    ],
)
def test_unproven_log_shapes_preserve_generic_schema(case: str) -> None:
    value = json.loads(json.dumps(payload()))
    if case == "missing":
        value.pop("log_json_shapes")
    elif case == "partial":
        value["log_json_shapes"][0]["refs"] = ["r0"]
    elif case == "mixed":
        value["source_catalog"][0]["kind"] = "block"
    elif case == "duplicate_paths":
        value["log_json_shapes"][0]["scalar_paths"] = [["requestID"], ["requestID"]]
    elif case == "too_many":
        value["parsing_policy"]["max_fields"] = 1
    elif case == "two_shapes":
        value["log_json_shapes"] *= 2
    else:
        value["log_json_shapes"][0]["offset"] = True
    base = json.loads(semantic_response_schema().schema_json)
    assert project_semantic_schema(base, json.dumps(value)) == base


def test_json_keys_remain_literals_not_schema_keywords() -> None:
    value = json.loads(json.dumps(payload()))
    value["log_json_shapes"][0]["scalar_paths"] = [["$ref"], ["type"], ["__class__"]]
    result = json.loads(
        semantic_response_schema().prepare_decoding(json.dumps(value)).schema_json
    )
    fields = result["properties"]["plan"]["anyOf"][0]["properties"]["fields"][
        "prefixItems"
    ]
    assert [
        field["properties"]["selector"]["const"]["path"][0]["name"] for field in fields
    ] == ["$ref", "type", "__class__"]


@pytest.mark.parametrize(
    "mutation", ["missing_field", "invented_path", "merged_events", "unknown_field_id"]
)
def test_projected_response_validation_rejects_ignored_native_constraints(
    mutation: str,
) -> None:
    value = {
        "schema_version": "1.0.0",
        "decision": "plan",
        "candidate_ids": [],
        "self_confidence": 0.99,
        "plan": {
            "kind": "log",
            "root_ref": None,
            "header_row": None,
            "data_start_row": None,
            "data_end_row": None,
            "footer_start_row": None,
            "repeated_header_rows": [],
            "scope": ["r0", "r1"],
            "fields": [
                {
                    "field_id": f"f{index}",
                    "semantic_name": f"value_{index}",
                    "semantic_type": "string",
                    "locale_hint": None,
                    "source_refs": ["r0", "r1"],
                    "selector": {
                        "kind": "log_json",
                        "index": None,
                        "offset": 0,
                        "path": [
                            {"operation": "key", "name": key, "occurrence": 0}
                            for key in path
                        ],
                        "value_source": None,
                        "delimiter": None,
                        "target": None,
                        "key_equals": None,
                    },
                }
                for index, path in enumerate([["requestID"], ["context", "status"]])
            ],
            "entities": [
                {
                    "entity_id": "records",
                    "entity_type": "events",
                    "parent_entity_id": None,
                    "field_ids": ["f0", "f1"],
                    "path": [],
                    "records": [["r0"], ["r1"]],
                }
            ],
        },
    }
    prepared = semantic_response_schema().prepare_decoding(json.dumps(payload()))
    prepared.validate(json.dumps(value))
    changed = json.loads(json.dumps(value))
    plan = changed["plan"]
    if mutation == "missing_field":
        plan["fields"].pop()
    elif mutation == "invented_path":
        plan["fields"][0]["selector"]["path"][0]["name"] = "timestamp"
    elif mutation == "merged_events":
        plan["entities"][0]["records"] = [["r0", "r1"]]
    else:
        plan["entities"][0]["field_ids"] = ["instance_id"]
    # Синтаксически допустимый DTO не доказывает сохранение всех полей/событий.
    LLMStructureSuggestion.model_validate_json(json.dumps(changed))
    with pytest.raises(LLMProviderError, match="LLM_SCHEMA_VIOLATION"):
        prepared.validate(json.dumps(changed))
