"""Детерминированные FK dependencies, SCC и консервативные join hints."""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from typing import Literal

from structuraguard.contracts.database import (
    DatabaseDependencyCycle,
    DatabaseDependencyGraph,
    ForeignKeyDependency,
    JoinTableCandidate,
    TableCatalog,
)

from ._database_catalog import CatalogInput, validated_metadata


def _components(
    nodes: tuple[str, ...],
    children: dict[str, tuple[str, ...]],
    parents: dict[str, tuple[str, ...]],
) -> list[tuple[str, ...]]:
    """Iterative Kosaraju: глубина схемы не ограничена Python recursion limit."""
    visited: set[str] = set()
    finished: list[str] = []
    for root in nodes:
        if root in visited:
            continue
        visited.add(root)
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(children[root]))]
        while stack:
            node, iterator = stack[-1]
            child = next(iterator, None)
            if child is None:
                finished.append(node)
                stack.pop()
            elif child not in visited:
                visited.add(child)
                stack.append((child, iter(children[child])))
    visited.clear()
    result: list[tuple[str, ...]] = []
    for root in reversed(finished):
        if root in visited:
            continue
        visited.add(root)
        pending = [root]
        members: list[str] = []
        while pending:
            node = pending.pop()
            members.append(node)
            for parent in parents[node]:
                if parent not in visited:
                    visited.add(parent)
                    pending.append(parent)
        result.append(tuple(members))
    return result


def _join_candidate(table: TableCatalog) -> JoinTableCandidate | None:
    metadata = table.inspection
    assert metadata is not None
    if metadata.kind != "table" or len(table.foreign_keys) != 2:
        return None
    first, second = sorted(table.foreign_keys, key=lambda key: key.foreign_key_id)
    if (
        first.referenced_table_id == second.referenced_table_id
        or table.table_id in {first.referenced_table_id, second.referenced_table_id}
        or set(first.column_ids) & set(second.column_ids)
    ):
        return None
    covered = set(first.column_ids) | set(second.column_ids)
    if covered != {column.column_id for column in table.columns}:
        return None
    if any(
        column.nullable
        or column.generated
        or column.inspection is None
        or column.inspection.identity is not None
        for column in table.columns
    ):
        return None
    if any(
        not constraint.validated or not constraint.enforced
        for constraint in metadata.constraints
        if constraint.kind in {"foreign_key", "primary_key", "unique"}
    ):
        return None
    key = table.primary_key
    key_kind: Literal["primary_key", "unique_constraint"] = "primary_key"
    if set(key) != covered:
        candidates = [key for key in table.unique_constraints if set(key) == covered]
        if not candidates:
            return None
        key = min(candidates)
        key_kind = "unique_constraint"
    return JoinTableCandidate(
        table_id=table.table_id,
        foreign_key_ids=(first.foreign_key_id, second.foreign_key_id),
        key_column_ids=key,
        key_kind=key_kind,
    )


def build_dependency_graph(catalog: CatalogInput) -> DatabaseDependencyGraph:
    """Построить parent→child graph; при любом цикле load_order отсутствует.

    SCC содержит всех участников и внутренние FK, включая composite pairs.
    Все объекты, включая views и изолированные tables, входят в граф. Load order
    отфильтрован по structural writable и не подтверждает DB permissions.
    Сложность O(V+E) без учёта детерминированной сортировки, без simple-cycle search.

    Args:
        catalog: Snapshot schema 1.1.0 или DatabaseCatalog catalog-v1;
            все FK targets и колонки должны находиться внутри каталога.

    Returns:
        DatabaseDependencyGraph с parent-first порядком для DAG либо SCC и
        ``requires_explicit_strategy=True``. Join candidates содержат только
        структурное evidence, без вывода о бизнес-смысле таблицы.

    Raises:
        DatabaseInspectionError: ``DATABASE_METADATA_UNSUPPORTED`` для
            неподдержанной версии каталога.
        pydantic.ValidationError: Некорректные metadata или незамкнутые FK.

    Side effects:
        Нет I/O, DDL/DML или выбора стратегии циклической загрузки. Caller
        ограничивает размер входа; функция не задаёт timeout/DB permissions.
    """
    snapshot = validated_metadata(catalog)
    tables = {
        table.table_id: table for schema in snapshot.schemas for table in schema.tables
    }

    def node_key(node: str) -> tuple[str, str, str]:
        table = tables[node]
        return table.schema_name, table.name, node

    nodes = tuple(sorted(tables, key=node_key))
    edges = tuple(
        sorted(
            (
                ForeignKeyDependency(
                    parent_table_id=key.referenced_table_id,
                    child_table_id=table.table_id,
                    foreign_key_id=key.foreign_key_id,
                    parent_column_ids=key.referenced_column_ids,
                    child_column_ids=key.column_ids,
                )
                for table in tables.values()
                for key in table.foreign_keys
            ),
            key=lambda edge: (
                node_key(edge.parent_table_id),
                node_key(edge.child_table_id),
                edge.foreign_key_id,
            ),
        )
    )
    children_sets: dict[str, set[str]] = {node: set() for node in nodes}
    parent_sets: dict[str, set[str]] = {node: set() for node in nodes}
    for edge in edges:
        children_sets[edge.parent_table_id].add(edge.child_table_id)
        parent_sets[edge.child_table_id].add(edge.parent_table_id)
    children = {
        node: tuple(sorted(values, key=node_key))
        for node, values in children_sets.items()
    }
    parents = {
        node: tuple(sorted(values, key=node_key))
        for node, values in parent_sets.items()
    }
    indegrees = {node: len(parents[node]) for node in nodes}
    ready = [node_key(node) for node in nodes if not indegrees[node]]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)[2]
        order.append(node)
        for child in children[node]:
            indegrees[child] -= 1
            if not indegrees[child]:
                heapq.heappush(ready, node_key(child))
    self_references = tuple(
        edge for edge in edges if edge.parent_table_id == edge.child_table_id
    )
    components = (
        _components(nodes, children, parents) if len(order) != len(nodes) else []
    )
    membership = {
        node: index for index, component in enumerate(components) for node in component
    }
    internal: dict[int, list[ForeignKeyDependency]] = {}
    for edge in edges:
        if (
            components
            and membership[edge.parent_table_id] == membership[edge.child_table_id]
        ):
            internal.setdefault(membership[edge.parent_table_id], []).append(edge)
    cycles = tuple(
        sorted(
            (
                DatabaseDependencyCycle(
                    table_ids=tuple(sorted(component, key=node_key)),
                    foreign_keys=tuple(internal[index]),
                )
                for index, component in enumerate(components)
                if index in internal
            ),
            key=lambda cycle: tuple(node_key(node) for node in cycle.table_ids),
        )
    )
    cycle_nodes = {node for cycle in cycles for node in cycle.table_ids}
    hints = tuple(
        candidate
        for node in nodes
        if node not in cycle_nodes
        and (candidate := _join_candidate(tables[node])) is not None
    )
    return DatabaseDependencyGraph(
        table_ids=nodes,
        edges=edges,
        topological_order=None if cycles else tuple(order),
        load_order=None
        if cycles
        else tuple(node for node in order if tables[node].writable),
        cycles=cycles,
        self_references=self_references,
        requires_explicit_strategy=bool(cycles),
        join_table_candidates=hints,
    )
