"""Wire response schema без coercion, defaults, extra fields и arbitrary text."""

import json

import pytest
from pydantic import ValidationError

from structuraguard.contracts.semantic_mapping import (
    SemanticAssessment,
    SemanticMappingDecision,
    SemanticMappingOptions,
)
from structuraguard.mapping import semantic_mapping_response_schema


@pytest.mark.parametrize(
    "score", [True, 1, 0.9, "NaN", "Infinity", "1e0", "1.000001", "0.9", "-0.000001"]
)
def test_self_score_requires_bounded_decimal_wire_string(score: object) -> None:
    with pytest.raises(ValidationError):
        SemanticAssessment.model_validate_json(
            json.dumps(
                {
                    "candidate_id": "c0",
                    "semantic_score": score,
                    "reason_code": "SEMANTIC_MATCH",
                }
            ),
            strict=True,
        )


def test_every_response_object_is_closed_and_required() -> None:
    schema = semantic_mapping_response_schema()
    assert schema.model is SemanticMappingDecision
    nodes: list[object] = [json.loads(schema.schema_json)]
    while nodes:
        node = nodes.pop()
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            nodes.extend(node.values())
        elif isinstance(node, list):
            nodes.extend(node)


@pytest.mark.parametrize(
    "candidate", ["public.customers.email", "SELECT 1", "exec()", "c9999999"]
)
def test_model_identifiers_are_opaque_bounded_tokens(candidate: str) -> None:
    with pytest.raises(ValidationError):
        SemanticAssessment(
            candidate_id=candidate,
            semantic_score="1.000000",
            reason_code="SEMANTIC_MATCH",
        )


@pytest.mark.parametrize("retention", ["raw", "all", True])
def test_raw_response_retention_cannot_be_enabled(retention: object) -> None:
    with pytest.raises(ValidationError):
        SemanticMappingOptions.model_validate({"response_retention": retention})
