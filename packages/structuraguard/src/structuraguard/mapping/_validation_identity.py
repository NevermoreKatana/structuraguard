"""Подтверждённые ключи: статистика и LLM hints не являются constraints."""

from structuraguard.contracts.common import LoadOperation
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    TableCatalog,
)
from structuraguard.contracts.mapping import FieldMapping
from structuraguard.contracts.mapping_rules import MappingIdentity, MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationPolicy

from ._validation_report import Issues


def generated_identity(column: ColumnCatalog) -> bool:
    metadata = column.inspection
    return metadata is not None and (
        metadata.identity is not None or metadata.rowid_alias
    )


def unique_keys(table: TableCatalog, issues: Issues) -> tuple[tuple[str, ...], ...]:
    meta = table.inspection
    if meta is None:
        return ()
    issues.work(
        sum(len(c.column_ids) + 1 for c in meta.constraints)
        + sum(len(i.keys) + 1 for i in meta.indexes)
        + sum(len(k) + 1 for k in table.unique_constraints)
        + len(table.primary_key)
    )
    blocked = {
        tuple(c.column_ids)
        for c in meta.constraints
        if c.kind in {"primary_key", "unique"}
        and (c.deferrable or not c.enforced or not c.validated)
    }
    keys = {
        k
        for k in (table.primary_key, *table.unique_constraints)
        if k and k not in blocked
    }
    for index in meta.indexes:
        key = tuple(k.column_id for k in index.keys if k.column_id is not None)
        if (
            index.unique
            and index.valid
            and index.predicate is None
            and len(key) == len(index.keys)
            and len(set(key)) == len(key)
            and key not in blocked
        ):
            keys.add(key)
    return tuple(sorted(keys))


def check_identity(
    table: TableCatalog,
    mappings: tuple[FieldMapping, ...],
    declarations: tuple[MappingIdentity, ...],
    operation: LoadOperation | None,
    policy: MappingValidationPolicy,
    location: MappingIssueLocation,
    issues: Issues,
) -> MappingIdentity | None:
    columns = {c.column_id: c for c in table.columns}
    covered = {
        m.target.column_id for m in mappings if m.target.table_id == table.table_id
    }
    keys = unique_keys(table, issues)
    declared = tuple(d for d in declarations if d.table_id == table.table_id)
    if len(declared) > 1:
        issues.add("MAPPING_IDENTITY_AMBIGUOUS", location)
        return None
    chosen = declared[0] if declared else None
    if chosen is None:
        allowed_pk = (
            table.primary_key
            and set(table.primary_key) <= covered
            and all(
                CatalogColumnRef(table_id=table.table_id, column_id=c)
                in policy.source_identity_allow
                for c in table.primary_key
            )
        )
        candidates = [
            k
            for k in keys
            if set(k) <= covered and all(not columns[c].nullable for c in k)
        ]
        if allowed_pk and table.primary_key in keys:
            chosen = MappingIdentity(
                table_id=table.table_id,
                kind="source_primary_key",
                column_ids=table.primary_key,
            )
        else:
            candidates = [k for k in candidates if k != table.primary_key]
            if len(candidates) > 1:
                issues.add("MAPPING_IDENTITY_AMBIGUOUS", location)
                return None
            if candidates:
                chosen = MappingIdentity(
                    table_id=table.table_id, kind="unique", column_ids=candidates[0]
                )
        if (
            chosen is None
            and operation is LoadOperation.INSERT_ONLY
            and table.primary_key
            and all(
                c not in covered and generated_identity(columns[c])
                for c in table.primary_key
            )
        ):
            chosen = MappingIdentity(
                table_id=table.table_id,
                kind="database_generated",
                column_ids=table.primary_key,
            )
    if chosen is None:
        issues.add("MAPPING_IDENTITY_REQUIRED", location)
        return None
    if any(c not in columns for c in chosen.column_ids):
        issues.add("MAPPING_UPSERT_KEY_INVALID", location)
        return None
    if chosen.kind == "database_generated":
        if (
            operation is not LoadOperation.INSERT_ONLY
            or chosen.column_ids != table.primary_key
            or any(
                c in covered or not generated_identity(columns[c])
                for c in chosen.column_ids
            )
        ):
            issues.add("MAPPING_UPSERT_KEY_INVALID", location)
        return chosen
    if chosen.column_ids not in keys or not set(chosen.column_ids) <= covered:
        issues.add("MAPPING_UPSERT_KEY_INVALID", location)
    if chosen.kind == "source_primary_key" and chosen.column_ids != table.primary_key:
        issues.add("MAPPING_UPSERT_KEY_INVALID", location)
    if chosen.kind == "natural_key" and chosen not in policy.natural_keys:
        issues.add("MAPPING_UPSERT_KEY_INVALID", location)
    if any(columns[c].nullable for c in chosen.column_ids):
        issues.add("MAPPING_IDENTITY_NULLABLE", location)
    if chosen.column_ids == table.primary_key and any(
        CatalogColumnRef(table_id=table.table_id, column_id=c)
        not in policy.source_identity_allow
        for c in chosen.column_ids
    ):
        issues.add("MAPPING_SOURCE_IDENTITY_FORBIDDEN", location)
    return chosen
