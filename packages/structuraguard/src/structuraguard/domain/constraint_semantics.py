"""Консервативные catalog predicates, общие для validator и read-only adapter."""

from dataclasses import dataclass

from structuraguard.contracts.database import DatabaseType, TableCatalog


@dataclass(frozen=True, slots=True)
class UniqueKey:
    column_ids: tuple[str, ...]
    nulls_equal: bool = False
    supported: bool = True


def base_type(data_type: DatabaseType) -> DatabaseType:
    while data_type.type_kind == "domain" and data_type.base_type is not None:
        data_type = data_type.base_type
    return data_type


def key_columns_supported(
    table: TableCatalog, columns: tuple[str, ...], dialect: str
) -> bool:
    """Не подменять неизвестные collations, affinity и operator classes Python ==."""
    by_id = {c.column_id: c for c in table.columns}
    if not columns or any(c not in by_id for c in columns) or table.inspection is None:
        return False
    for cid in columns:
        column = by_id[cid]
        if column.inspection is None or column.generated:
            return False
        dt = column.inspection.data_type
        if dt.type_kind in ("domain", "enum", "array", "unknown"):
            return False
        native = dt.native_type.casefold()
        if dt.canonical_type == "integer":
            if dialect == "postgresql" and native not in (
                "smallint",
                "int2",
                "integer",
                "int4",
                "bigint",
                "int8",
            ):
                return False
        elif dt.canonical_type == "decimal":
            if dialect != "postgresql" or native not in ("decimal", "numeric"):
                return False
        elif dt.canonical_type in ("boolean", "date", "datetime", "uuid"):
            if dialect != "postgresql":
                return False
            if dt.canonical_type == "datetime" and dt.timezone is not True:
                return False
        elif dt.canonical_type == "text":
            indexes = [
                i
                for i in table.inspection.indexes
                if tuple(k.column_id for k in i.keys) == columns
                and i.unique
                and i.valid
                and i.predicate is None
            ]
            collations = {
                k.collation for i in indexes for k in i.keys if k.column_id == cid
            }
            allowed = (
                {"BINARY"}
                if dialect == "sqlite"
                else {"pg_catalog.C", "pg_catalog.POSIX", "C", "POSIX"}
            )
            if not collations or not collations <= allowed:
                return False
            if dialect == "postgresql" and native not in (
                "text",
                "varchar",
                "character varying",
            ):
                return False
        else:
            return False
    return True


def unique_keys(table: TableCatalog, dialect: str) -> tuple[UniqueKey, ...]:
    """Выдать даже неподдержанные indexes: их нельзя молча потерять из отчёта."""
    declared = set(table.unique_constraints)
    if table.primary_key:
        declared.add(table.primary_key)
    declared.update((c.column_id,) for c in table.columns if c.unique)
    output: list[UniqueKey] = []
    metadata = table.inspection
    indexes = () if metadata is None else metadata.indexes
    for index in indexes:
        if not index.unique:
            continue
        columns = tuple(k.column_id for k in index.keys if k.column_id is not None)
        supported = (
            index.valid
            and index.predicate is None
            and len(columns) == len(index.keys)
            and index.method in (None, "btree")
            and all(
                k.operator_class
                in (
                    None,
                    "pg_catalog.int2_ops",
                    "pg_catalog.int4_ops",
                    "pg_catalog.int8_ops",
                    "pg_catalog.numeric_ops",
                    "pg_catalog.text_ops",
                    "pg_catalog.bool_ops",
                    "pg_catalog.date_ops",
                    "pg_catalog.timestamptz_ops",
                    "pg_catalog.uuid_ops",
                )
                for k in index.keys
            )
        )
        output.append(
            UniqueKey(
                columns,
                index.nulls_not_distinct,
                supported and key_columns_supported(table, columns, dialect),
            )
        )
        if supported:
            declared.discard(columns)
    for columns in sorted(declared):
        output.append(
            UniqueKey(columns, False, key_columns_supported(table, columns, dialect))
        )
    if metadata is not None:
        deferred = {
            c.column_ids
            for c in metadata.constraints
            if c.kind in ("unique", "primary_key")
            and (c.deferrable or not c.enforced or not c.validated)
        }
        output = [
            UniqueKey(
                k.column_ids,
                k.nulls_equal,
                k.supported and k.column_ids not in deferred,
            )
            for k in output
        ]
    return tuple(dict.fromkeys(output))
