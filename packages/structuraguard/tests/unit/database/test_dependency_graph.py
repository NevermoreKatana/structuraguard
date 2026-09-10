"""FK graph: DAG, SCC, composite links и осторожные association hints."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from structuraguard.contracts import (
    ColumnCatalog,
    ColumnInspectionMetadata,
    DatabaseMetadataSnapshot,
    DatabaseType,
    ForeignKeyCatalog,
    SchemaCatalog,
    TableCatalog,
    TableInspectionMetadata,
)
from structuraguard.domain.database_graph import build_dependency_graph


def snapshot(count: int, pairs: list[tuple[int, int]]) -> DatabaseMetadataSnapshot:
    tables = []
    for node in range(count):
        name = f"t{node:04d}"
        table = TableCatalog(
            table_id=name,
            schema_name="app",
            name=name,
            columns=(
                ColumnCatalog(
                    column_id="id",
                    name="id",
                    type_name="integer",
                    nullable=False,
                    primary_key=True,
                    inspection=ColumnInspectionMetadata(
                        data_type=DatabaseType(
                            native_type="INTEGER", canonical_type="integer"
                        ),
                        ordinal_position=0,
                    ),
                ),
            ),
            primary_key=("id",),
            inspection=TableInspectionMetadata(),
            foreign_keys=tuple(
                ForeignKeyCatalog(
                    foreign_key_id=f"fk{index}",
                    column_ids=("id",),
                    referenced_table_id=f"t{parent:04d}",
                    referenced_column_ids=("id",),
                )
                for index, (parent, child) in enumerate(pairs)
                if child == node
            ),
        )
        tables.append(table)
    return DatabaseMetadataSnapshot(
        dialect="sqlite",
        target_id="test",
        target_policy_fingerprint="sha256:" + "a" * 64,
        comments_supported=False,
        schemas=(SchemaCatalog(schema_id="app", name="app", tables=tuple(tables)),),
    )


@given(
    st.integers(min_value=0, max_value=30),
    st.sets(st.tuples(st.integers(0, 29), st.integers(0, 29)), max_size=120),
)
def test_arbitrary_dag_has_complete_parent_first_order(
    count: int, pairs: set[tuple[int, int]]
) -> None:
    catalog = snapshot(count, sorted((a, b) for a, b in pairs if a < b < count))
    graph = build_dependency_graph(catalog)
    assert not graph.requires_explicit_strategy and graph.cycles == ()
    assert graph.topological_order == graph.load_order
    assert graph.load_order is not None
    positions = {node: index for index, node in enumerate(graph.load_order)}
    assert len(positions) == count
    for edge in graph.edges:
        assert positions[edge.parent_table_id] < positions[edge.child_table_id]
    reversed_catalog = catalog.model_copy(
        update={
            "schemas": (
                catalog.schemas[0].model_copy(
                    update={
                        "tables": tuple(
                            table.model_copy(
                                update={
                                    "foreign_keys": tuple(reversed(table.foreign_keys))
                                }
                            )
                            for table in reversed(catalog.schemas[0].tables)
                        ),
                    }
                ),
            )
        }
    )
    assert build_dependency_graph(reversed_catalog) == graph


def test_diamond_parallel_links_and_isolated_nodes() -> None:
    graph = build_dependency_graph(
        snapshot(5, [(0, 1), (0, 2), (1, 3), (2, 3), (2, 3)])
    )
    assert graph.load_order == tuple(f"t{node:04d}" for node in range(5))
    assert len(graph.edges) == 5


def test_composite_fk_and_namespaces(
    catalog_snapshot: DatabaseMetadataSnapshot,
) -> None:
    graph = build_dependency_graph(catalog_snapshot)
    assert graph.load_order == ("parent", "child")
    (edge,) = graph.edges
    assert edge.parent_column_ids == ("b", "a")
    assert edge.child_column_ids == ("right", "left")
    child_schema, parent_schema = catalog_snapshot.schemas
    renamed = child_schema.model_copy(
        update={
            "tables": (child_schema.tables[0].model_copy(update={"name": "parent"}),)
        }
    )
    assert (
        build_dependency_graph(
            catalog_snapshot.model_copy(update={"schemas": (parent_schema, renamed)})
        ).load_order
        == graph.load_order
    )


@given(st.integers(min_value=1, max_value=60))
def test_ring_is_one_scc_with_all_fk_evidence(count: int) -> None:
    graph = build_dependency_graph(
        snapshot(count, [(i, (i + 1) % count) for i in range(count)])
    )
    assert graph.topological_order is None and graph.load_order is None
    assert graph.requires_explicit_strategy
    (component,) = graph.cycles
    assert component.table_ids == graph.table_ids
    assert component.foreign_keys == graph.edges
    assert len(graph.self_references) == (1 if count == 1 else 0)


def test_scc_does_not_include_downstream_or_unrelated_tables() -> None:
    graph = build_dependency_graph(
        snapshot(7, [(0, 1), (1, 0), (1, 2), (3, 3), (3, 4), (5, 6)])
    )
    assert tuple(cycle.table_ids for cycle in graph.cycles) == (
        ("t0000", "t0001"),
        ("t0003",),
    )
    assert graph.self_references[0].child_table_id == "t0003"
    assert graph.load_order is None


def test_deep_cycle_does_not_require_recursive_dfs() -> None:
    count = 1500
    graph = build_dependency_graph(
        snapshot(count, [(i, (i + 1) % count) for i in range(count)])
    )
    assert len(graph.cycles[0].table_ids) == count


def test_views_remain_nodes_but_are_not_load_targets() -> None:
    catalog = snapshot(2, [])
    first, second = catalog.schemas[0].tables
    view = second.model_copy(
        update={
            "writable": False,
            "inspection": TableInspectionMetadata(kind="view"),
            "columns": tuple(
                column.model_copy(update={"writable": False})
                for column in second.columns
            ),
        }
    )
    graph = build_dependency_graph(
        catalog.model_copy(
            update={
                "schemas": (
                    catalog.schemas[0].model_copy(update={"tables": (first, view)}),
                )
            }
        )
    )
    assert graph.table_ids == ("t0000", "t0001") and graph.load_order == ("t0000",)


def join_snapshot() -> DatabaseMetadataSnapshot:
    catalog = snapshot(3, [])
    first, second, association = catalog.schemas[0].tables
    template = association.columns[0]
    columns = (
        tuple(
            template.model_copy(
                update={
                    "column_id": name,
                    "name": name,
                    "inspection": template.inspection.model_copy(
                        update={"ordinal_position": i}
                    ),
                }
            )
            for i, name in enumerate(("left", "right"))
        )
        if template.inspection
        else ()
    )
    association = association.model_copy(
        update={
            "columns": columns,
            "primary_key": ("left", "right"),
            "foreign_keys": (
                ForeignKeyCatalog(
                    foreign_key_id="left_fk",
                    column_ids=("left",),
                    referenced_table_id=first.table_id,
                    referenced_column_ids=("id",),
                ),
                ForeignKeyCatalog(
                    foreign_key_id="right_fk",
                    column_ids=("right",),
                    referenced_table_id=second.table_id,
                    referenced_column_ids=("id",),
                ),
            ),
        }
    )
    return catalog.model_copy(
        update={
            "schemas": (
                catalog.schemas[0].model_copy(
                    update={"tables": (first, second, association)}
                ),
            )
        }
    )


def test_join_hint_requires_full_structural_evidence() -> None:
    graph = build_dependency_graph(join_snapshot())
    (candidate,) = graph.join_table_candidates
    assert candidate.foreign_key_ids == ("left_fk", "right_fk")
    assert candidate.key_column_ids == ("left", "right")
    assert candidate.key_kind == "primary_key"


@pytest.mark.parametrize(
    "change",
    [
        "payload",
        "surrogate",
        "same_parent",
        "nullable",
        "no_unique",
        "name_only",
        "self",
    ],
)
def test_insufficient_join_evidence_is_not_guessed(change: str) -> None:
    catalog = join_snapshot()
    first, second, association = catalog.schemas[0].tables
    if change in {"payload", "surrogate"}:
        extra = first.columns[0].model_copy(
            update={"primary_key": change == "surrogate"}
        )
        columns = (*association.columns, extra)
        if change == "surrogate":
            columns = tuple(
                column.model_copy(update={"primary_key": column.column_id == "id"})
                for column in columns
            )
        association = association.model_copy(
            update={
                "columns": columns,
                "primary_key": ("id",)
                if change == "surrogate"
                else association.primary_key,
            }
        )
    elif change in {"same_parent", "self"}:
        left, right = association.foreign_keys
        right = right.model_copy(
            update={
                "referenced_table_id": first.table_id
                if change == "same_parent"
                else association.table_id,
                "referenced_column_ids": ("id",)
                if change == "same_parent"
                else ("left",),
            }
        )
        association = association.model_copy(update={"foreign_keys": (left, right)})
    elif change in {"nullable", "no_unique"}:
        association = association.model_copy(
            update={
                "primary_key": (),
                "columns": tuple(
                    column.model_copy(
                        update={"primary_key": False, "nullable": change == "nullable"}
                    )
                    for column in association.columns
                ),
            }
        )
    else:
        association = association.model_copy(
            update={"name": "users_groups", "foreign_keys": ()}
        )
    changed = catalog.model_copy(
        update={
            "schemas": (
                catalog.schemas[0].model_copy(
                    update={"tables": (first, second, association)}
                ),
            )
        }
    )
    assert not build_dependency_graph(changed).join_table_candidates
