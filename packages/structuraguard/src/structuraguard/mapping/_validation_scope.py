"""Exact scope и writability по инертной metadata, без SQL compilation."""

from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    DatabaseCatalog,
    TableCatalog,
)
from structuraguard.contracts.mapping import FieldMapping
from structuraguard.contracts.mapping_rules import MappingIssueLocation
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.normalized import NormalizedDatasetManifest

from ._validation_report import Issues


def name_key(value: str, dialect: str) -> str:
    return (
        value.translate(
            str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
        )
        if dialect == "sqlite"
        else value
    )


def system_object(table: TableCatalog, dialect: str) -> bool:
    schema = name_key(table.schema_name, dialect)
    name = name_key(table.name, dialect)
    if dialect == "sqlite":
        return schema != "main" or name.startswith("sqlite_")
    return schema == "information_schema" or schema.startswith("pg_")


def table_allowed(
    table: TableCatalog, policy: MappingValidationPolicy, dialect: str
) -> bool:
    schema = name_key(table.schema_name, dialect)
    pair = (schema, name_key(table.name, dialect))
    return (
        schema in {name_key(s, dialect) for s in policy.allow_schemas}
        and schema not in {name_key(s, dialect) for s in policy.deny_schemas}
        and pair
        in {
            (name_key(s, dialect), name_key(t, dialect)) for s, t in policy.allow_tables
        }
        and pair
        not in {
            (name_key(s, dialect), name_key(t, dialect)) for s, t in policy.deny_tables
        }
        and not system_object(table, dialect)
    )


def check_policy(
    catalog: DatabaseCatalog, policy: MappingValidationPolicy, issues: Issues
) -> None:
    schemas = {name_key(s.name, catalog.dialect) for s in catalog.schemas}
    tables = {
        (name_key(t.schema_name, catalog.dialect), name_key(t.name, catalog.dialect))
        for s in catalog.schemas
        for t in s.tables
    }
    for index, name in enumerate((*policy.allow_schemas, *policy.deny_schemas)):
        issues.work()
        if name_key(name, catalog.dialect) not in schemas:
            issues.add(
                "MAPPING_SCHEMA_NOT_FOUND",
                MappingIssueLocation(section="policy", index=index),
            )
    for index, (schema, table) in enumerate(
        (*policy.allow_tables, *policy.deny_tables)
    ):
        issues.work()
        if (
            name_key(schema, catalog.dialect),
            name_key(table, catalog.dialect),
        ) not in tables:
            issues.add(
                "MAPPING_SCOPE_INVALID",
                MappingIssueLocation(section="policy", index=index, component=1),
            )
    refs = {
        CatalogColumnRef(table_id=t.table_id, column_id=c.column_id)
        for s in catalog.schemas
        for t in s.tables
        for c in t.columns
    }
    for index, key in enumerate(policy.natural_keys):
        if any(
            CatalogColumnRef(table_id=key.table_id, column_id=c) not in refs
            for c in key.column_ids
        ):
            issues.add(
                "MAPPING_SCOPE_INVALID",
                MappingIssueLocation(section="policy", index=index, component=3),
            )
    for index, ref in enumerate(
        (
            *policy.scope.allow,
            *policy.scope.deny,
            *policy.source_identity_allow,
            *policy.lookup_allow,
        )
    ):
        issues.work()
        if ref not in refs:
            issues.add(
                "MAPPING_SCOPE_INVALID",
                MappingIssueLocation(section="policy", index=index, component=2),
            )


def check_mapping(
    index: int,
    mapping: FieldMapping,
    manifest: NormalizedDatasetManifest,
    tables: dict[str, TableCatalog],
    policy: MappingValidationPolicy,
    dialect: str,
    issues: Issues,
) -> tuple[TableCatalog, ColumnCatalog] | None:
    loc = MappingIssueLocation(section="mappings", index=index)
    if manifest.semantic_type_for(mapping.source) is None:
        issues.add("MAPPING_SOURCE_NOT_FOUND", loc)
    table = tables.get(mapping.target.table_id)
    if table is None:
        issues.add("MAPPING_TABLE_NOT_FOUND", loc)
        return None
    if system_object(table, dialect):
        issues.add("MAPPING_SYSTEM_OBJECT_FORBIDDEN", loc)
    schema = name_key(table.schema_name, dialect)
    if schema not in {name_key(s, dialect) for s in policy.allow_schemas} or schema in {
        name_key(s, dialect) for s in policy.deny_schemas
    }:
        issues.add("MAPPING_SCHEMA_DENIED", loc)
    pair = (schema, name_key(table.name, dialect))
    if pair not in {
        (name_key(s, dialect), name_key(t, dialect)) for s, t in policy.allow_tables
    } or pair in {
        (name_key(s, dialect), name_key(t, dialect)) for s, t in policy.deny_tables
    }:
        issues.add("MAPPING_TABLE_DENIED", loc)
    if (
        not table.writable
        or table.inspection is None
        or table.inspection.kind != "table"
    ):
        issues.add("MAPPING_TABLE_NOT_WRITABLE", loc)
    column = next(
        (c for c in table.columns if c.column_id == mapping.target.column_id), None
    )
    if column is None:
        issues.add("MAPPING_COLUMN_NOT_FOUND", loc)
        return None
    if mapping.target not in policy.scope.allow or mapping.target in policy.scope.deny:
        issues.add("MAPPING_COLUMN_DENIED", loc)
    if column.generated:
        issues.add("MAPPING_GENERATED_COLUMN", loc)
    if not column.writable:
        issues.add("MAPPING_COLUMN_NOT_WRITABLE", loc)
    if (
        column.primary_key
        or (
            column.inspection is not None
            and (
                column.inspection.identity is not None or column.inspection.rowid_alias
            )
        )
    ) and mapping.target not in policy.source_identity_allow:
        issues.add("MAPPING_SOURCE_IDENTITY_FORBIDDEN", loc)
    return table, column


def required(column: ColumnCatalog) -> bool:
    meta = column.inspection
    non_null = not column.nullable
    domain_default = False
    data_type = meta.data_type if meta is not None else None
    while data_type is not None:
        non_null = non_null or data_type.domain_not_null
        domain_default = domain_default or data_type.domain_default is not None
        data_type = data_type.base_type
    return (
        non_null
        and not column.generated
        and (
            meta is None
            or (
                meta.default is None
                and meta.identity is None
                and not meta.rowid_alias
                and not domain_default
            )
        )
    )
