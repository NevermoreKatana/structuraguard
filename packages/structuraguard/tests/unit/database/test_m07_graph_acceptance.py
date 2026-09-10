"""K5: oracle для произвольных SCC и независимые отрицательные join cases."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.database.test_dependency_graph import join_snapshot, snapshot

from structuraguard.contracts import (
    ConstraintInspectionMetadata,
    DatabaseMetadataSnapshot,
    TableCatalog,
    TableInspectionMetadata,
)
from structuraguard.domain import build_dependency_graph


@given(st.sets(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=32))
def test_arbitrary_graph_scc_matches_transitive_closure(
    pairs: set[tuple[int, int]],
) -> None:
    catalog = snapshot(8, sorted(pairs))
    graph = build_dependency_graph(catalog)
    reachable = [
        [(parent, child) in pairs for child in range(8)] for parent in range(8)
    ]
    # Независимый oracle Floyd–Warshall; production использует iterative DFS.
    for middle in range(8):
        for parent in range(8):
            for child in range(8):
                reachable[parent][child] |= (
                    reachable[parent][middle] and reachable[middle][child]
                )
    expected = {
        frozenset(
            f"t{child:04d}"
            for child in range(8)
            if reachable[parent][child] and reachable[child][parent]
        )
        for parent in range(8)
        if reachable[parent][parent]
    }
    assert {frozenset(cycle.table_ids) for cycle in graph.cycles} == expected
    assert graph.requires_explicit_strategy == bool(expected)
    shuffled = catalog.model_copy(
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
    assert build_dependency_graph(shuffled) == graph
    for cycle in graph.cycles:
        assert set(cycle.foreign_keys) == {
            edge
            for edge in graph.edges
            if edge.parent_table_id in cycle.table_ids
            and edge.child_table_id in cycle.table_ids
        }


def replace_join(
    catalog: DatabaseMetadataSnapshot, table: TableCatalog
) -> DatabaseMetadataSnapshot:
    first, second, _ = catalog.schemas[0].tables
    return catalog.model_copy(
        update={
            "schemas": (
                catalog.schemas[0].model_copy(
                    update={"tables": (first, second, table)}
                ),
            )
        }
    )


@pytest.mark.parametrize("nullable", [False, True])
def test_join_unique_key_requires_nonnullable_endpoints(nullable: bool) -> None:
    catalog = join_snapshot()
    table = catalog.schemas[0].tables[-1]
    table = table.model_copy(
        update={
            "primary_key": (),
            "unique_constraints": (("left", "right"),),
            "columns": tuple(
                column.model_copy(update={"primary_key": False, "nullable": nullable})
                for column in table.columns
            ),
        }
    )
    candidates = build_dependency_graph(
        replace_join(catalog, table)
    ).join_table_candidates
    assert bool(candidates) is not nullable
    if candidates:
        assert candidates[0].key_kind == "unique_constraint"


@pytest.mark.parametrize("case", ["overlap", "generated", "unvalidated", "unenforced"])
def test_join_rejects_each_insufficient_evidence_independently(case: str) -> None:
    catalog = join_snapshot()
    table = catalog.schemas[0].tables[-1]
    if case == "overlap":
        table = table.model_copy(
            update={
                "columns": (table.columns[0],),
                "primary_key": ("left",),
                "foreign_keys": tuple(
                    key.model_copy(update={"column_ids": ("left",)})
                    for key in table.foreign_keys
                ),
            }
        )
    elif case == "generated":
        first, second = table.columns
        assert first.inspection is not None
        first = first.model_copy(
            update={
                "generated": True,
                "writable": False,
                "inspection": first.inspection.model_copy(
                    update={
                        "generation_expression": "right * 2",
                        "generation_storage": "stored",
                    }
                ),
            }
        )
        table = table.model_copy(update={"columns": (first, second)})
    else:
        constraint = ConstraintInspectionMetadata(
            name="left_fk",
            kind="foreign_key",
            column_ids=("left",),
            validated=case != "unvalidated",
            enforced=case != "unenforced",
        )
        table = table.model_copy(
            update={"inspection": TableInspectionMetadata(constraints=(constraint,))}
        )
    assert not build_dependency_graph(
        replace_join(catalog, table)
    ).join_table_candidates
