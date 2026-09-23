"""Публичный pipeline сохраняет названия source для SDK mapper без смены ID."""

import json

import pytest
from tests.fakes.pipeline import Stream

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts.common import SemanticParsingMode
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser, JsonDocumentParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SDKDependencies, SourceRequest


def engine() -> AsyncStructuraGuard:
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    registry.register(JsonDocumentParser())
    return AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=SDKDependencies(
            security=SecurityPolicy(
                allowed_formats=("csv", "json"), parser_trust="trusted"
            ),
            parsing=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        ),
    )


@pytest.mark.anyio
@pytest.mark.parametrize("header", ["postal_codde", "postal-code", "email_0"])
async def test_csv_header_is_bound_to_profile_without_stripping_ids(
    header: str,
) -> None:
    sdk = engine()
    data = f"email,{header}\na@example.test,101000\nb@example.test,190000\n"
    source = await sdk.inspect_source(
        SourceRequest(stream=Stream(data.encode()), display_name="contacts.csv")
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan is not None
        normalized = await sdk.parse_semantically(source, plan=plan)
        before = normalized.manifest.normalized_fingerprint
        profile = await sdk.profile_records(normalized)
        names = {f.field.field_name: f.source_names for f in profile.fields}
        assert names[plan.fields[0].semantic_name] == ("email",)
        assert names[plan.fields[1].semantic_name] == (header,)
        assert plan.fields[1].semantic_name != header
        assert profile.parse_plan_fingerprint == plan.fingerprint
        assert profile.normalized_manifest_fingerprint == before
        assert normalized.manifest.normalized_fingerprint == before
        assert await sdk.profile_records(normalized) == profile
        assert all(
            label.origin == "observed_location"
            for field in profile.fields
            for label in field.labels
            if label.kind == "source_name"
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "key", ["postal_codde", "postal-code", "postal/code", "postal~code"]
)
async def test_json_uses_literal_key_not_generated_field_name(key: str) -> None:
    sdk = engine()
    data = json.dumps([{key: "101000"}, {key: "190000"}]).encode()
    source = await sdk.inspect_source(
        SourceRequest(stream=Stream(data), display_name="contacts.json")
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan is not None
        normalized = await sdk.parse_semantically(source, plan=plan)
        profile = await sdk.profile_records(normalized)
        assert profile.fields[0].field.field_name == "field_0"
        assert profile.fields[0].source_names == (key,)


@pytest.mark.anyio
async def test_long_key_is_not_truncated_into_a_different_source_name() -> None:
    sdk = engine()
    key = "я" * 129
    source = await sdk.inspect_source(
        SourceRequest(
            stream=Stream(json.dumps([{key: "101000"}, {key: "190000"}]).encode()),
            display_name="contacts.json",
        )
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan is not None
        normalized = await sdk.parse_semantically(source, plan=plan)
        profile = await sdk.profile_records(normalized)
        assert profile.fields[0].source_names == ()


@pytest.mark.anyio
async def test_nested_key_path_does_not_claim_a_flat_leaf_alias() -> None:
    sdk = engine()
    data = b'[{"billing":{"code":"101000"}},{"billing":{"code":"190000"}}]'
    source = await sdk.inspect_source(
        SourceRequest(stream=Stream(data), display_name="contacts.json")
    )
    async with source:
        plan = await sdk.create_parse_plan(source)
        assert plan is not None
        normalized = await sdk.parse_semantically(source, plan=plan)
        profile = await sdk.profile_records(normalized)
        assert profile.fields[0].source_names == ()
