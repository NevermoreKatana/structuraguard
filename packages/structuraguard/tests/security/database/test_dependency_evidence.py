"""Подмена SCC/order evidence не превращает циклический graph в load plan."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    DatabaseDependencyCycle,
    DatabaseDependencyGraph,
    ForeignKeyDependency,
)


def edge(parent: str, child: str) -> ForeignKeyDependency:
    return ForeignKeyDependency(
        parent_table_id=parent,
        child_table_id=child,
        foreign_key_id=f"{parent}-{child}",
        parent_column_ids=("id",),
        child_column_ids=("parent",),
    )


def test_self_reference_cannot_claim_acyclic_load_order() -> None:
    key = edge("a", "a")
    with pytest.raises(ValidationError):
        DatabaseDependencyGraph(
            table_ids=("a",),
            edges=(key,),
            self_references=(key,),
            topological_order=("a",),
            load_order=("a",),
        )


def test_disconnected_cycles_cannot_claim_one_scc() -> None:
    with pytest.raises(ValidationError):
        DatabaseDependencyCycle(
            table_ids=("a", "b"), foreign_keys=(edge("a", "a"), edge("b", "b"))
        )


def test_omitted_scc_is_rejected() -> None:
    left = edge("a", "a")
    with pytest.raises(ValidationError):
        DatabaseDependencyGraph(
            table_ids=("a", "b", "c"),
            edges=(left, edge("b", "c"), edge("c", "b")),
            self_references=(left,),
            cycles=(DatabaseDependencyCycle(table_ids=("a",), foreign_keys=(left,)),),
            requires_explicit_strategy=True,
            topological_order=None,
            load_order=None,
        )


def test_scc_must_include_every_internal_fk() -> None:
    first, second, self_key = edge("a", "b"), edge("b", "a"), edge("a", "a")
    with pytest.raises(ValidationError):
        DatabaseDependencyGraph(
            table_ids=("a", "b"),
            edges=(first, second, self_key),
            self_references=(self_key,),
            cycles=(
                DatabaseDependencyCycle(
                    table_ids=("a", "b"), foreign_keys=(first, second)
                ),
            ),
            requires_explicit_strategy=True,
            topological_order=None,
            load_order=None,
        )


def test_cyclic_graph_cannot_publish_partial_order() -> None:
    key = edge("a", "a")
    with pytest.raises(ValidationError):
        DatabaseDependencyGraph(
            table_ids=("a", "unrelated"),
            edges=(key,),
            self_references=(key,),
            cycles=(DatabaseDependencyCycle(table_ids=("a",), foreign_keys=(key,)),),
            requires_explicit_strategy=True,
            topological_order=("unrelated",),
            load_order=("unrelated",),
        )
