"""Canonical schema identity не зависит от порядка reflection и runtime binding."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from structuraguard.contracts import DatabaseMetadataSnapshot
from structuraguard.domain.database_fingerprint import (
    canonical_database_catalog,
    database_fingerprint,
    verify_database_fingerprint,
)
from structuraguard.exceptions import DatabaseInspectionError


def reorder(snapshot: DatabaseMetadataSnapshot, seed: int) -> DatabaseMetadataSnapshot:
    import random

    randomizer = random.Random(seed)

    def shuffle[T](values: tuple[T, ...]) -> tuple[T, ...]:
        output = list(values)
        randomizer.shuffle(output)
        return tuple(output)

    schemas = []
    for schema in snapshot.schemas:
        tables = []
        for table in schema.tables:
            assert table.inspection is not None
            metadata = table.inspection.model_copy(
                update={
                    "indexes": shuffle(table.inspection.indexes),
                    "constraints": shuffle(table.inspection.constraints),
                }
            )
            tables.append(
                table.model_copy(
                    update={
                        "columns": shuffle(table.columns),
                        "foreign_keys": shuffle(table.foreign_keys),
                        "unique_constraints": shuffle(table.unique_constraints),
                        "check_constraints": shuffle(table.check_constraints),
                        "inspection": metadata,
                    }
                )
            )
        schemas.append(schema.model_copy(update={"tables": shuffle(tuple(tables))}))
    return snapshot.model_copy(update={"schemas": shuffle(tuple(schemas))})


def test_reflection_permutations_preserve_sha256(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    expected = database_fingerprint(catalog_snapshot)

    @given(st.integers())
    def check(seed: int) -> None:
        assert database_fingerprint(reorder(catalog_snapshot, seed)) == expected

    check()
    assert len(expected) == 71 and expected.startswith("sha256:")


def test_volatile_fields_and_opaque_ids_are_excluded(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    raw = catalog_snapshot.model_dump_json()
    changed = raw.replace('"target_id":"fixture"', '"target_id":"elsewhere"').replace(
        '"sha256:' + "a" * 64 + '"', '"sha256:' + "b" * 64 + '"'
    )
    payload = json.loads(changed)
    for schema in payload["schemas"]:
        schema["schema_id"] += "-volatile"
        for table in schema["tables"]:
            table["table_id"] += "-volatile"
            table["writable"] = False
            for column in table["columns"]:
                column["writable"] = False
            for fk in table["foreign_keys"]:
                fk["foreign_key_id"] += "-volatile"
                fk["referenced_table_id"] += "-volatile"
            for index in table["inspection"]["indexes"]:
                index["index_id"] += "-volatile"
    assert database_fingerprint(
        DatabaseMetadataSnapshot.model_validate(payload)
    ) == database_fingerprint(catalog_snapshot)


@pytest.mark.parametrize(
    "change",
    [
        "nullable",
        "default",
        "native_type",
        "comment",
        "check",
        "fk_action",
        "pk_order",
        "fk_pairs",
        "index",
        "ordinal",
        "capability",
    ],
)
def test_schema_mutations_change_fingerprint(
    catalog_snapshot: DatabaseMetadataSnapshot, change: str
) -> None:
    payload = json.loads(catalog_snapshot.model_dump_json())
    table = payload["schemas"][0]["tables"][0]
    column = table["columns"][1]
    if change == "nullable":
        column["nullable"] = True
    elif change == "default":
        column["inspection"]["default"] = "42"
    elif change == "native_type":
        column["inspection"]["data_type"]["native_type"] = "BIGINT"
    elif change == "comment":
        column["comment"] = "Новое значение"
    elif change == "check":
        table["check_constraints"][0] = "left > 2"
    elif change == "fk_action":
        table["foreign_keys"][0]["inspection"]["on_delete"] = "RESTRICT"
    elif change == "pk_order":
        payload["schemas"][1]["tables"][0]["primary_key"].reverse()
    elif change == "fk_pairs":
        table["foreign_keys"][0]["referenced_column_ids"].reverse()
    elif change == "index":
        table["inspection"]["indexes"][0]["keys"][0]["descending"] = True
    elif change == "ordinal":
        column["inspection"]["ordinal_position"] = 3
    elif change == "capability":
        payload["comments_supported"] = True
    changed = DatabaseMetadataSnapshot.model_validate(payload)
    assert database_fingerprint(changed) != database_fingerprint(catalog_snapshot)
    with pytest.raises(DatabaseInspectionError) as caught:
        verify_database_fingerprint(changed, database_fingerprint(catalog_snapshot))
    assert caught.value.error_code == "DATABASE_SCHEMA_DRIFT"


def test_projection_does_not_publish_runtime_identity(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    projection = canonical_database_catalog(catalog_snapshot)
    assert '"format":"catalog-v1"' in projection
    assert "target_id" not in projection and "fingerprint" not in projection
    assert '"writable"' not in projection
    verify_database_fingerprint(
        catalog_snapshot, database_fingerprint(catalog_snapshot)
    )


def test_catalog_v1_golden_bytes_and_hash(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    assert (
        database_fingerprint(catalog_snapshot)
        == "sha256:637d99b066e50aaad54ab597a944e2653834c584802c1b5a90f14a249e42d988"
    )
    empty = catalog_snapshot.model_copy(update={"schemas": ()})
    assert (
        canonical_database_catalog(empty)
        == '{"comments_supported":false,"dialect":"sqlite","format":"catalog-v1","schemas":[]}'
    )
    assert (
        database_fingerprint(empty)
        == "sha256:8c3ec1408cbd35f0db4171b88d003eed6cc3481917359f8fdd9f5713e3c02947"
    )


def test_column_ids_are_resolved_to_names(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    payload = json.loads(catalog_snapshot.model_dump_json())
    for schema in payload["schemas"]:
        for table in schema["tables"]:
            for column in table["columns"]:
                column["column_id"] += "-opaque"
            table["primary_key"] = [key + "-opaque" for key in table["primary_key"]]
            table["unique_constraints"] = [
                [key + "-opaque" for key in constraint]
                for constraint in table["unique_constraints"]
            ]
            for key in table["foreign_keys"]:
                key["column_ids"] = [column + "-opaque" for column in key["column_ids"]]
                key["referenced_column_ids"] = [
                    column + "-opaque" for column in key["referenced_column_ids"]
                ]
            for index in table["inspection"]["indexes"]:
                for key in index["keys"]:
                    key["column_id"] += "-opaque"
    assert database_fingerprint(
        DatabaseMetadataSnapshot.model_validate(payload)
    ) == database_fingerprint(catalog_snapshot)


@pytest.mark.parametrize(
    "attribute",
    [
        "enum_labels",
        "domain_default",
        "domain_not_null",
        "domain_checks",
        "element_type",
    ],
)
def test_nested_type_semantics_affect_hash(
    catalog_snapshot: DatabaseMetadataSnapshot, attribute: str
) -> None:
    payload = json.loads(catalog_snapshot.model_dump_json())
    metadata = payload["schemas"][0]["tables"][0]["columns"][1]["inspection"][
        "data_type"
    ]
    values: dict[str, object] = {
        "enum_labels": ["first", "second"],
        "domain_default": "10",
        "domain_not_null": True,
        "domain_checks": ["VALUE > 0", "VALUE < 100"],
        "element_type": {"native_type": "INTEGER", "canonical_type": "integer"},
    }
    metadata[attribute] = values[attribute]
    changed = DatabaseMetadataSnapshot.model_validate(payload)
    assert database_fingerprint(changed) != database_fingerprint(catalog_snapshot)
    if attribute in {"enum_labels", "domain_checks"}:
        metadata[attribute].reverse()
        reversed_hash = database_fingerprint(
            DatabaseMetadataSnapshot.model_validate(payload)
        )
        assert (reversed_hash == database_fingerprint(changed)) == (
            attribute == "domain_checks"
        )
