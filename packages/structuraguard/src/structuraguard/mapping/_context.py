"""Frozen anchors и проверяемое structural/FK evidence без global assignment."""

from dataclasses import dataclass
from decimal import Decimal

from structuraguard.contracts.database import (
    CatalogColumnRef,
    DatabaseDependencyGraph,
    ForeignKeyDependency,
)
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import FieldRelationship, NormalizedFieldProfile

from ._aliases import context_similarity
from ._inputs import Budget
from ._ranking import Source, Target
from ._scores import average, ratio


@dataclass(frozen=True)
class ContextEvidence:
    structure: Decimal = Decimal(0)
    graph: Decimal = Decimal(0)
    structure_available: bool = False
    graph_available: bool = False
    blockers: tuple[str, ...] = ()
    foreign_keys: tuple[str, ...] = ()


class MappingContext:
    """Индексы одного вызова; anchors не обновляются после context scoring."""

    def __init__(
        self,
        fields: tuple[NormalizedFieldProfile, ...],
        relationships: tuple[FieldRelationship, ...],
        graph: DatabaseDependencyGraph,
        anchors: dict[SemanticFieldRef, CatalogColumnRef],
        allowed: frozenset[CatalogColumnRef],
        budget: Budget,
    ) -> None:
        budget.work(len(fields) + len(relationships))
        self.anchors = anchors
        self.fields = {f.field: f for f in fields}
        self.relationships: dict[SemanticFieldRef, list[FieldRelationship]] = {
            f.field: [] for f in fields
        }
        for relation in relationships:
            if not relation.count:
                continue
            self.relationships[relation.left].append(relation)
            if relation.right != relation.left:
                self.relationships[relation.right].append(relation)
        self.edges: dict[tuple[str, str], list[ForeignKeyDependency]] = {}
        self.child_edges: dict[str, list[ForeignKeyDependency]] = {}
        for edge in graph.edges:
            budget.work(1 + len(edge.parent_column_ids) + len(edge.child_column_ids))
            # Scope ограничивает положительное evidence, но не снимает FK
            # с разрешённой child-колонки при недоступном parent mapping.
            self.child_edges.setdefault(edge.child_table_id, []).append(edge)
            pairs = tuple(
                CatalogColumnRef(table_id=edge.parent_table_id, column_id=c)
                for c in edge.parent_column_ids
            )
            pairs += tuple(
                CatalogColumnRef(table_id=edge.child_table_id, column_id=c)
                for c in edge.child_column_ids
            )
            if all(ref in allowed for ref in pairs):
                self.edges.setdefault(
                    (edge.parent_table_id, edge.child_table_id), []
                ).append(edge)
        self.cyclic = frozenset(
            (key.child_table_id, key.foreign_key_id)
            for cycle in graph.cycles
            for key in cycle.foreign_keys
        )
        self.budget = budget
        budget.retain(
            len(relationships) * 512 + len(graph.edges) * 512 + len(fields) * 1024
        )

    def _full_pair_evidence(
        self,
        relation: FieldRelationship,
        edge: ForeignKeyDependency,
        source: SemanticFieldRef,
        target: CatalogColumnRef,
    ) -> bool:
        self.budget.work(1 + len(self.anchors))
        assignments = self.anchors | {source: target}
        parent_entity, child_entity = (
            relation.left.entity_type,
            relation.right.entity_type,
        )
        for parent, child in zip(
            edge.parent_column_ids, edge.child_column_ids, strict=True
        ):
            self.budget.work(1 + 2 * len(assignments))
            parent_ref = CatalogColumnRef(
                table_id=edge.parent_table_id, column_id=parent
            )
            child_ref = CatalogColumnRef(table_id=edge.child_table_id, column_id=child)
            parents = {
                s
                for s, ref in assignments.items()
                if s.entity_type == parent_entity and ref == parent_ref
            }
            children = {
                s
                for s, ref in assignments.items()
                if s.entity_type == child_entity and ref == child_ref
            }
            if not parents or not children:
                return False
            self.budget.work(len(parents))
            self.budget.work(sum(len(self.relationships[p]) for p in parents))
            if not any(
                r.kind == "parent_child" and r.left in parents and r.right in children
                for p in parents
                for r in self.relationships[p]
            ):
                return False
        return True

    def score(self, source: Source, target: Target) -> ContextEvidence:
        ref = source.profile.field
        relationships = self.relationships[ref]
        self.budget.work(
            len(source.context) * len(target.table_names) + 3 * len(relationships)
        )
        neighbors = {
            r.right if r.left == ref else r.left
            for r in relationships
            if r.kind == "co_occurrence" and r.left.entity_type == r.right.entity_type
        }
        neighbor_hits = sum(
            self.anchors.get(n) is not None
            and self.anchors[n].table_id == target.table.table_id
            for n in neighbors
            if n != ref
        )
        structure = max(
            ratio(neighbor_hits, len(neighbors)),
            max(
                (
                    context_similarity(label, target.table_names)
                    for label in source.context
                ),
                default=Decimal(0),
            ),
        )
        relation_scores: list[Decimal] = []
        evidence: set[str] = set()
        blockers: set[str] = set()
        for relation in relationships:
            if relation.kind != "parent_child":
                continue
            parent = (
                target.ref if relation.left == ref else self.anchors.get(relation.left)
            )
            child = (
                target.ref
                if relation.right == ref
                else self.anchors.get(relation.right)
            )
            best = Decimal(0)
            if parent is not None and child is not None:
                for edge in self.edges.get((parent.table_id, child.table_id), ()):
                    self.budget.work()
                    evidence.add(edge.foreign_key_id)
                    full = self._full_pair_evidence(relation, edge, ref, target.ref)
                    best = max(best, Decimal(1) if full else Decimal("0.50"))
                    if not full:
                        blockers.add("FK_UNRESOLVED")
                    if (edge.child_table_id, edge.foreign_key_id) in self.cyclic:
                        blockers.add("FK_STRATEGY_REQUIRED")
            relation_scores.append(best)
        # FK candidate требует отдельной стратегии даже без source parent_child.
        for edge in self.child_edges.get(target.table.table_id, ()):
            self.budget.work(1 + len(edge.child_column_ids))
            if target.column.column_id in edge.child_column_ids:
                blockers.add("FK_UNRESOLVED")
                if (edge.child_table_id, edge.foreign_key_id) in self.cyclic:
                    blockers.add("FK_STRATEGY_REQUIRED")
        if structure and any(
            r in source.profile.reasons for r in ("context_limit", "pair_limit")
        ):
            blockers.add("CONTEXT_EVIDENCE_INCOMPLETE")
        graph = average(tuple(relation_scores))
        return ContextEvidence(
            structure,
            graph,
            bool(source.context or neighbors),
            bool(relation_scores),
            tuple(sorted(blockers)),
            tuple(sorted(evidence)),
        )

    def identity_evidence(self, source: Source, target: Target) -> tuple[str, ...]:
        if target.semantic_table is None or not target.semantic_table.identity_keys:
            return ()
        result = {"IDENTITY_KEY_DECLARED"}
        table = target.table
        self.budget.work(1 + len(table.columns) + len(self.anchors))
        names = {c.name: c for c in table.columns}
        assignments = self.anchors | {source.profile.field: target.ref}
        self.budget.work(len(table.unique_constraints))
        constraint_width = len(table.primary_key) + sum(
            len(key) for key in table.unique_constraints
        )
        for key in target.semantic_table.identity_keys:
            # Даже неподдержанная декларация требует обхода ключа и DB constraints.
            self.budget.work(1 + 3 * len(key) + constraint_width)
            columns = tuple(names[name] for name in key)
            ids = tuple(c.column_id for c in columns)
            if ids != table.primary_key and ids not in table.unique_constraints:
                continue
            if any(c.nullable or c.generated for c in columns):
                continue
            for column in columns:
                self.budget.work(1 + len(assignments))
                column_ref = CatalogColumnRef(
                    table_id=table.table_id, column_id=column.column_id
                )
                found = [
                    self.fields[f]
                    for f, ref in assignments.items()
                    if f.entity_type == source.profile.field.entity_type
                    and ref == column_ref
                ]
                if (
                    len(found) != 1
                    or found[0].unique_mode != "exact"
                    or found[0].null_count
                    or not found[0].non_null_count
                    or found[0].unique_count != found[0].non_null_count
                ):
                    break
            else:
                result.add("IDENTITY_KEY_SUPPORTED")
        return tuple(sorted(result))
