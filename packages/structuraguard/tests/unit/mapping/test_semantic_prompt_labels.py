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
