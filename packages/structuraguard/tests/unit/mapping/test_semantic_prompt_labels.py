"""Имена исходных полей не вытесняются координатами из bounded LLM prompt."""

import json

import pytest
from tests.fakes.mapping import (
    catalog,
    column,
    profile,
    rehash_profile,
    scope_for,
    table,
)

from structuraguard.contracts.profiling import ProfileLabel
from structuraguard.mapping import prepare_semantic_mapping


@pytest.mark.anyio
async def test_verified_source_name_precedes_repeated_json_parent_locations() -> None:
    data = await profile()
    field = data.fields[0]
    locations = tuple(
        ProfileLabel(
            field=field.field,
            kind="json_parent",
            text=f"/{i}",
            origin="observed_location",
        )
        for i in range(6)
    )
    source_name = ProfileLabel(
        field=field.field, kind="source_name", text="email", origin="observed_location"
    )
    data = rehash_profile(
        data,
        fields=(field.model_copy(update={"labels": (*locations, source_name)}),),
    )
    db = catalog(table("contacts", column("email")))
    prepared = await prepare_semantic_mapping(data, db, scope=scope_for(db))
    payload = json.loads(prepared.groups[0].payload_json)
    labels = payload["fields"][0]["labels"]
    assert labels[0] == "email"
    assert len(labels) == 4
    assert labels[1:] == ["/0", "/1", "/2"]


@pytest.mark.anyio
async def test_prompt_separates_source_tasks_from_candidate_options() -> None:
    from tests.fakes.semantic_mapping import decision_for

    from structuraguard.contracts.common import StringScalar
    from structuraguard.contracts.semantic_mapping import SemanticMappingOptions
    from structuraguard.exceptions import MappingError
    from structuraguard.mapping import semantic_mapping_prompt
    from structuraguard.mapping._semantic_validation import validate_decision

    data = await profile(
        {
            "field_0": StringScalar(value="00123"),
            "field_1": StringScalar(value="anna@example.test"),
        }
    )
    names = {"field_0": "postal_codde", "field_1": "email"}
    data = rehash_profile(
        data,
        fields=tuple(
            field.model_copy(
                update={
                    "labels": (
                        ProfileLabel(
                            field=field.field,
                            kind="source_name",
                            text=names[field.field.field_name],
                            origin="observed_location",
                        ),
                    )
                }
            )
            for field in data.fields
        ),
    )
    db = catalog(table("contacts", column("email"), column("postal_code", position=1)))
    prepared = await prepare_semantic_mapping(data, db, scope=scope_for(db))
    group = prepared.groups[0]
    payload = json.loads(group.payload_json)
    assert not {"columns", "tables", "relations"} & payload.keys()
    assert {
        "column_candidates",
        "table_candidates",
        "relation_candidates",
    } <= payload.keys()
    assert "\n" not in group.payload_json
    for field in payload["fields"]:
        assert field["name"] == names[field["semantic_field_name"]]
        assert field["candidate_ids"] == [
            candidate["candidate_id"]
            for candidate in payload["column_candidates"]
            if candidate["source_id"] == field["source_id"]
        ]
    for entity in payload["entities"]:
        assert entity["candidate_ids"] == [
            candidate["candidate_id"]
            for candidate in payload["table_candidates"]
            if candidate["source_id"] == entity["source_id"]
        ]
    prompt = semantic_mapping_prompt()
    assert prompt.identity.version == "1.3.0"
    assert "compact SINGLE-LINE JSON" in prompt.text
    assert "with its honest semantic score" in prompt.text
    assert "with a low score instead" not in prompt.text
    decision = decision_for(group)
    assert len(decision.columns) == len(payload["fields"]) == 2
    assert (
        validate_decision(decision.canonical_json(), group, SemanticMappingOptions())
        == decision
    )
    duplicated = decision.model_copy(
        update={"columns": (*decision.columns, decision.columns[0])}
    )
    with pytest.raises(MappingError, match="SEMANTIC_MAPPING_DECISION_INVALID"):
        validate_decision(duplicated.canonical_json(), group, SemanticMappingOptions())
