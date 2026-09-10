"""K4: недостающие поля projection и volatile metadata полного каталога."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.database.test_fingerprint import reorder

from structuraguard.contracts import (
    DatabaseCatalog,
    DatabaseMetadataSnapshot,
    ProducerMetadata,
)
from structuraguard.domain import build_dependency_graph, database_fingerprint


def complete_snapshot(snapshot: DatabaseMetadataSnapshot) -> DatabaseMetadataSnapshot:
    payload = json.loads(snapshot.model_dump_json())
    table = payload["schemas"][0]["tables"][0]
    table["inspection"]["constraints"] = [
        {
            "name": "positive_left",
            "kind": "check",
            "column_ids": ["left"],
            "expression": "left > 0",
        },
        {
            "name": "positive_right",
            "kind": "check",
            "column_ids": ["right"],
            "expression": "right > 0",
        },
    ]
    table["inspection"]["indexes"][0]["keys"].append({"column_id": "right"})
    table["inspection"]["indexes"].append(
        {
            "index_id": "expression_index",
            "name": "absolute_left",
            "unique": False,
            "origin": "index",
            "keys": [{"expression": "abs(left)"}],
        }
    )
    table["foreign_keys"].append(
        {**table["foreign_keys"][0], "foreign_key_id": "second_link"}
    )
    return DatabaseMetadataSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("section", "updates"),
    [
        ("snapshot", {"dialect": "postgresql"}),
        ("schema", {"comment": "Описание схемы"}),
        ("table", {"name": "renamed_child"}),
        ("table", {"comment": "Описание таблицы"}),
        ("table", {"unique_constraints": [["right", "left"]]}),
        ("column", {"name": "renamed_left"}),
        ("column", {"unique": True}),
        ("type", {"length": 32}),
        ("type", {"precision": 12}),
        ("type", {"scale": 2}),
        ("type", {"timezone": True}),
        ("type", {"affinity": "text"}),
        ("type", {"type_kind": "builtin"}),
        (
            "type",
            {"base_type": {"native_type": "SMALLINT", "canonical_type": "integer"}},
        ),
        ("column_metadata", {"rowid_alias": True}),
        ("column_metadata", {"identity": "by_default"}),
        ("table_metadata", {"partitioned": True}),
        ("table_metadata", {"strict": True}),
        ("table_metadata", {"without_rowid": True}),
        ("fk", {"name": "renamed_fk"}),
        ("fk", {"on_update": "CASCADE"}),
        ("fk", {"match": "FULL"}),
        ("fk", {"deferrable": True}),
        ("fk", {"initially_deferred": True}),
        ("fk", {"on_delete_column_ids": ["left"]}),
        ("index", {"name": "renamed_index"}),
        ("index", {"unique": True}),
        ("index", {"predicate": "left > 10"}),
        ("index", {"origin": "unique_constraint"}),
        ("index", {"include_column_ids": ["id"]}),
        ("index", {"method": "btree"}),
        ("index", {"nulls_not_distinct": True}),
        ("index", {"valid": False}),
        ("index", {"comment": "Описание индекса"}),
        ("index", {"keys": [{"column_id": "right"}, {"column_id": "left"}]}),
        ("index_key", {"column_id": "id"}),
        ("index_key", {"collation": "C"}),
        ("index_key", {"nulls_first": True}),
        ("index_key", {"operator_class": "integer_ops"}),
        ("expression_key", {"expression": "abs(right)"}),
        ("constraint", {"name": "renamed_check"}),
        ("constraint", {"kind": "not_null"}),
        ("constraint", {"column_ids": ["right"]}),
        ("constraint", {"expression": "left > 10"}),
        ("constraint", {"comment": "Описание ограничения"}),
        ("constraint", {"deferrable": True}),
        ("constraint", {"initially_deferred": True}),
        ("constraint", {"validated": False}),
        ("constraint", {"enforced": False}),
        ("constraint", {"no_inherit": True}),
    ],
)
def test_remaining_projection_fields_change_hash(
    catalog_snapshot: DatabaseMetadataSnapshot,
    section: str,
    updates: dict[str, object],
) -> None:
    baseline = complete_snapshot(catalog_snapshot)
    payload = json.loads(baseline.model_dump_json())
    schema = payload["schemas"][0]
    table = schema["tables"][0]
    column = table["columns"][1]
    metadata = table["inspection"]
    sections = {
        "snapshot": payload,
        "schema": schema,
        "table": table,
        "column": column,
        "type": column["inspection"]["data_type"],
        "column_metadata": column["inspection"],
        "table_metadata": metadata,
        "fk": table["foreign_keys"][0]["inspection"],
        "index": metadata["indexes"][0],
        "index_key": metadata["indexes"][0]["keys"][0],
        "expression_key": metadata["indexes"][2]["keys"][0],
        "constraint": metadata["constraints"][0],
    }
    sections[section].update(updates)
    changed = DatabaseMetadataSnapshot.model_validate(payload)
    assert database_fingerprint(changed) != database_fingerprint(baseline)


@pytest.mark.parametrize(
    "change",
    [
        "schema_name",
        "canonical_type",
        "generated",
        "generation_storage",
        "autoincrement",
        "kind",
        "fk_target",
    ],
)
def test_coupled_schema_fields_change_hash(
    catalog_snapshot: DatabaseMetadataSnapshot, change: str
) -> None:
    payload = json.loads(catalog_snapshot.model_dump_json())
    table = payload["schemas"][0]["tables"][0]
    column = table["columns"][1]
    if change == "generation_storage":
        column.update(generated=True, writable=False)
        column["inspection"].update(
            generation_expression="right * 2", generation_storage="stored"
        )
    if change == "autoincrement":
        column["inspection"]["rowid_alias"] = True
    baseline = DatabaseMetadataSnapshot.model_validate(payload)
    if change == "schema_name":
        payload["schemas"][0]["name"] = "renamed_app"
        table["schema_name"] = "renamed_app"
    elif change == "canonical_type":
        column["type_name"] = "unknown"
        column["inspection"]["data_type"]["canonical_type"] = "unknown"
    elif change == "generated":
        column.update(generated=True, writable=False)
        column["inspection"].update(
            generation_expression="right * 2", generation_storage="stored"
        )
    elif change == "generation_storage":
        column["inspection"]["generation_storage"] = "virtual"
    elif change == "autoincrement":
        column["inspection"]["autoincrement"] = True
    elif change == "kind":
        table["inspection"]["kind"] = "view"
        table["writable"] = False
        for item in table["columns"]:
            item["writable"] = False
    else:
        parent = payload["schemas"][1]["tables"][0]
        second = {**parent, "table_id": "other_parent", "name": "other_parent"}
        payload["schemas"][1]["tables"].append(second)
        baseline = DatabaseMetadataSnapshot.model_validate(payload)
        table["foreign_keys"][0]["referenced_table_id"] = "other_parent"
    assert database_fingerprint(
        DatabaseMetadataSnapshot.model_validate(payload)
    ) != database_fingerprint(baseline)


def test_named_constraints_and_parallel_fk_reflection_permutations(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    catalog = complete_snapshot(catalog_snapshot)
    expected = database_fingerprint(catalog)

    @given(st.integers())
    def check(seed: int) -> None:
        assert database_fingerprint(reorder(catalog, seed)) == expected

    check()


def test_full_catalog_producer_and_display_metadata_do_not_change_hash(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    catalog = DatabaseCatalog(
        schema_version="1.1.0",
        fingerprint_version="catalog-v1",
        dialect=catalog_snapshot.dialect,
        target_id=catalog_snapshot.target_id,
        target_policy_fingerprint=catalog_snapshot.target_policy_fingerprint,
        schemas=catalog_snapshot.schemas,
        comments_supported=catalog_snapshot.comments_supported,
        producer=ProducerMetadata(
            component_id="inspector", component_version="1.0.0", sdk_version="0.3.0"
        ),
        dependency_graph=build_dependency_graph(catalog_snapshot),
        database_fingerprint=database_fingerprint(catalog_snapshot),
    )
    changed = DatabaseCatalog.model_validate(
        {
            **catalog.model_dump(),
            "producer": ProducerMetadata(
                component_id="other", component_version="9.0.0", sdk_version="1.0.0"
            ),
            "database_name": "other display name",
            "database_fingerprint": "sha256:" + "f" * 64,
        }
    )
    assert database_fingerprint(changed) == database_fingerprint(catalog)
