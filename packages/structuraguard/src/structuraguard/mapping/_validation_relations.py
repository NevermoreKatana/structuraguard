"""Разрешить полные FK descriptors; строки и generated keys не читаются."""

from structuraguard.contracts.database import (
    CatalogColumnRef,
    DatabaseCatalog,
    DatabaseType,
    ForeignKeyDependency,
    TableCatalog,
)
from structuraguard.contracts.mapping import FieldMapping
from structuraguard.contracts.mapping_rules import MappingIssueLocation, MappingRelation
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.normalized import NormalizedDatasetManifest
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.domain.database_graph import dependency_order

from ._validation_identity import unique_keys
from ._validation_report import Issues
from ._validation_scope import required, table_allowed


def _fk_types_compatible(left: DatabaseType, right: DatabaseType, dialect: str) -> bool:
    if left == right:
        return True
    # PostgreSQL допускает FK между int2/int4/int8. Значения каждой записываемой
    # колонки отдельно проходят check_type; их диапазоны здесь не расширяются.
    return dialect == "postgresql" and all(
        data_type.type_kind in {None, "builtin"}
        and data_type.canonical_type == "integer"
        and data_type.native_type.casefold()
        in {"smallint", "int2", "integer", "int4", "bigint", "int8"}
        for data_type in (left, right)
    )


def check_relations(
    mappings: tuple[FieldMapping, ...],
    relations: tuple[tuple[int, MappingRelation], ...],
    tables: dict[str, TableCatalog],
    catalog: DatabaseCatalog,
    manifest: NormalizedDatasetManifest,
    profile: NormalizedDataProfile | None,
    policy: MappingValidationPolicy,
    issues: Issues,
) -> tuple[str, ...]:
    targets = {m.target: m.source for m in mappings}
    selected = {m.target.table_id for m in mappings} & tables.keys()
    resolved: set[tuple[str, str]] = set()
    relation_edges: list[ForeignKeyDependency] = []
    for index, relation in relations:
        loc = MappingIssueLocation(section="relations", index=index)
        issues.work()
        child = tables.get(relation.child_table_id)
        parent = tables.get(relation.parent_table_id)
        if child is None or parent is None:
            issues.add("MAPPING_TABLE_NOT_FOUND", loc)
            continue
        fk = next(
            (
                f
                for f in child.foreign_keys
                if f.foreign_key_id == relation.foreign_key_id
            ),
            None,
        )
        if fk is None:
            issues.add("MAPPING_FK_NOT_FOUND", loc)
            continue
        key = (child.table_id, fk.foreign_key_id)
        if key in resolved:
            issues.add("MAPPING_RELATION_UNRESOLVED", loc)
        if (fk.referenced_table_id, fk.column_ids, fk.referenced_column_ids) != (
            parent.table_id,
            relation.child_column_ids,
            relation.parent_column_ids,
        ):
            issues.add("MAPPING_FK_PAIR_MISMATCH", loc)
            continue
        if relation.strategy in {"generated_key", "deferred", "two_phase"}:
            issues.add("MAPPING_RELATION_STRATEGY_UNSUPPORTED", loc)
            continue
        if child.table_id not in selected or not table_allowed(
            parent, policy, catalog.dialect
        ):
            issues.add("MAPPING_RELATION_UNRESOLVED", loc)
        child_refs = tuple(
            CatalogColumnRef(table_id=child.table_id, column_id=c)
            for c in fk.column_ids
        )
        parent_refs = tuple(
            CatalogColumnRef(table_id=parent.table_id, column_id=c)
            for c in fk.referenced_column_ids
        )
        if any(ref in policy.scope.deny for ref in parent_refs):
            issues.add("MAPPING_COLUMN_DENIED", loc)
        if tuple(targets.get(ref) for ref in child_refs) != relation.child_sources:
            issues.add("MAPPING_FK_PAIR_MISMATCH", loc)
        if any(
            manifest.semantic_type_for(source) is None
            for source in (*relation.child_sources, *relation.parent_sources)
        ):
            issues.add("MAPPING_SOURCE_NOT_FOUND", loc)
        for child_id, parent_id in zip(
            fk.column_ids, fk.referenced_column_ids, strict=True
        ):
            issues.work(len(child.columns) + len(parent.columns))
            left = next(c for c in child.columns if c.column_id == child_id)
            right = next(c for c in parent.columns if c.column_id == parent_id)
            if (
                left.inspection is None
                or right.inspection is None
                or not _fk_types_compatible(
                    left.inspection.data_type,
                    right.inspection.data_type,
                    catalog.dialect,
                )
            ):
                issues.add("MAPPING_TYPE_INCOMPATIBLE", loc)
        if relation.strategy == "mapped_parent":
            if (
                parent.table_id not in selected
                or tuple(targets.get(ref) for ref in parent_refs)
                != relation.parent_sources
            ):
                issues.add("MAPPING_RELATION_UNRESOLVED", loc)
            if len(relation.parent_sources) != len(relation.child_sources):
                issues.add("MAPPING_FK_PAIR_MISMATCH", loc)
            else:
                for parent_source, child_source in zip(
                    relation.parent_sources, relation.child_sources, strict=True
                ):
                    if parent_source.entity_type == child_source.entity_type:
                        continue
                    issues.work(
                        len(profile.relationships) + 1 if profile is not None else 1
                    )
                    evidence = profile is not None and any(
                        r.left == parent_source
                        and r.right == child_source
                        and r.count > 0
                        and r.kind in {"co_occurrence", "parent_child"}
                        for r in profile.relationships
                    )
                    if (
                        parent_source.entity_type != child_source.entity_type
                        and not evidence
                    ):
                        issues.add("MAPPING_RELATION_UNRESOLVED", loc)
        else:
            if (
                relation.parent_sources
                or fk.referenced_column_ids not in unique_keys(parent, issues)
                or any(ref not in policy.lookup_allow for ref in parent_refs)
            ):
                issues.add("MAPPING_RELATION_UNRESOLVED", loc)
        resolved.add(key)
        relation_edges.append(
            ForeignKeyDependency(
                parent_table_id=parent.table_id,
                child_table_id=child.table_id,
                foreign_key_id=fk.foreign_key_id,
                parent_column_ids=fk.referenced_column_ids,
                child_column_ids=fk.column_ids,
            )
        )
    edges: list[ForeignKeyDependency] = []
    for ordinal, table_id in enumerate(sorted(selected)):
        table = tables[table_id]
        for component, fk in enumerate(
            sorted(table.foreign_keys, key=lambda f: f.foreign_key_id)
        ):
            issues.work(len(table.columns) * len(fk.column_ids) + 1)
            cols = [c for c in table.columns if c.column_id in fk.column_ids]
            supplied = any(
                CatalogColumnRef(table_id=table_id, column_id=c.column_id) in targets
                for c in cols
            )
            if supplied or any(required(c) for c in cols):
                if (table_id, fk.foreign_key_id) not in resolved:
                    issues.add(
                        "MAPPING_RELATION_UNRESOLVED",
                        MappingIssueLocation(
                            section="catalog", index=ordinal, component=component
                        ),
                    )
                edges.append(
                    ForeignKeyDependency(
                        parent_table_id=fk.referenced_table_id,
                        child_table_id=table_id,
                        foreign_key_id=fk.foreign_key_id,
                        parent_column_ids=fk.referenced_column_ids,
                        child_column_ids=fk.column_ids,
                    )
                )
    # Split требует связности выбранных таблиц; одних одинаковых source имён мало.
    by_entity: dict[str, set[str]] = {}
    for mapping in mappings:
        if mapping.target.table_id in selected:
            by_entity.setdefault(mapping.source.entity_type, set()).add(
                mapping.target.table_id
            )
    for group in by_entity.values():
        reached = {min(group)}
        while True:
            issues.work(len(relation_edges) + 1)
            extended = (
                reached
                | {
                    e.child_table_id
                    for e in relation_edges
                    if e.parent_table_id in reached and e.child_table_id in group
                }
                | {
                    e.parent_table_id
                    for e in relation_edges
                    if e.child_table_id in reached and e.parent_table_id in group
                }
            )
            if reached == extended:
                break
            reached = extended
        if reached != group:
            issues.add("MAPPING_RELATION_UNRESOLVED")
    order = dependency_order(
        {t: (tables[t].schema_name, tables[t].name, t) for t in selected}, tuple(edges)
    )
    if order is None:
        issues.add("CYCLIC_DEPENDENCY_REQUIRES_STRATEGY")
    return order or ()
