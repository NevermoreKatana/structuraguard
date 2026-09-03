"""Декларативные контракты каталога БД и безопасной загрузки."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from ._base import FrozenContract
from .common import (
    ErrorPolicy,
    FingerprintStr,
    IdentifierStr,
    ProducerMetadata,
    SchemaVersionStr,
    UtcDateTime,
    _safe_text,
)

_ShortText = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=4096,
    ),
    AfterValidator(_safe_text),
]
_PositiveInt = Annotated[StrictInt, Field(gt=0)]


class ColumnCatalog(FrozenContract):
    """Столбец каталога без connection metadata и секретов."""

    column_id: IdentifierStr
    name: IdentifierStr
    type_name: IdentifierStr
    nullable: StrictBool
    primary_key: StrictBool = False
    unique: StrictBool = False
    generated: StrictBool = False
    writable: StrictBool = True
    comment: _ShortText | None = None

    @model_validator(mode="after")
    def validate_column_flags(self) -> Self:
        if self.primary_key and self.nullable:
            raise ValueError("primary key column cannot be nullable")
        if self.generated and self.writable:
            raise ValueError("generated column cannot be marked writable")
        return self


class ForeignKeyCatalog(FrozenContract):
    """Проверяемая ссылка внешнего ключа на catalog identifiers."""

    foreign_key_id: IdentifierStr
    column_ids: tuple[IdentifierStr, ...]
    referenced_table_id: IdentifierStr
    referenced_column_ids: tuple[IdentifierStr, ...]

    @model_validator(mode="after")
    def validate_column_pairs(self) -> Self:
        if not self.column_ids:
            raise ValueError("foreign key must contain at least one column")
        if len(self.column_ids) != len(self.referenced_column_ids):
            raise ValueError("foreign key column counts must match")
        if len(set(self.column_ids)) != len(self.column_ids):
            raise ValueError("foreign key contains duplicate local columns")
        if len(set(self.referenced_column_ids)) != len(self.referenced_column_ids):
            raise ValueError("foreign key contains duplicate referenced columns")
        return self


class TableCatalog(FrozenContract):
    """Таблица и её ограничения в стабильном каталожном представлении."""

    table_id: IdentifierStr
    schema_name: IdentifierStr
    name: IdentifierStr
    columns: tuple[ColumnCatalog, ...]
    primary_key: tuple[IdentifierStr, ...] = ()
    foreign_keys: tuple[ForeignKeyCatalog, ...] = ()
    unique_constraints: tuple[tuple[IdentifierStr, ...], ...] = ()
    check_constraints: tuple[_ShortText, ...] = ()
    writable: StrictBool = True
    comment: _ShortText | None = None

    @model_validator(mode="after")
    def validate_table_references(self) -> Self:
        if not self.columns:
            raise ValueError("table catalog must contain at least one column")

        column_ids = tuple(column.column_id for column in self.columns)
        column_names = tuple(column.name for column in self.columns)
        if len(set(column_ids)) != len(column_ids):
            raise ValueError("table catalog contains duplicate column identifiers")
        if len(set(column_names)) != len(column_names):
            raise ValueError("table catalog contains duplicate column names")

        known_columns = set(column_ids)
        if not set(self.primary_key) <= known_columns:
            raise ValueError("primary key references an unknown column")
        if len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("primary key contains duplicate columns")
        flagged_primary_key = {
            column.column_id for column in self.columns if column.primary_key
        }
        if flagged_primary_key != set(self.primary_key):
            raise ValueError("column and table primary key declarations disagree")

        foreign_key_ids = tuple(item.foreign_key_id for item in self.foreign_keys)
        if len(set(foreign_key_ids)) != len(foreign_key_ids):
            raise ValueError("table catalog contains duplicate foreign key identifiers")
        for foreign_key in self.foreign_keys:
            if not set(foreign_key.column_ids) <= known_columns:
                raise ValueError("foreign key references an unknown local column")

        seen_unique: set[tuple[str, ...]] = set()
        for constraint in self.unique_constraints:
            if not constraint:
                raise ValueError("unique constraint cannot be empty")
            if len(set(constraint)) != len(constraint):
                raise ValueError("unique constraint contains duplicate columns")
            if not set(constraint) <= known_columns:
                raise ValueError("unique constraint references an unknown column")
            if constraint in seen_unique:
                raise ValueError("table catalog contains duplicate unique constraints")
            seen_unique.add(constraint)
        return self


class SchemaCatalog(FrozenContract):
    """Схема БД с уникальными stable table identifiers."""

    schema_id: IdentifierStr
    name: IdentifierStr
    tables: tuple[TableCatalog, ...] = ()

    @model_validator(mode="after")
    def validate_tables(self) -> Self:
        table_ids = tuple(table.table_id for table in self.tables)
        table_names = tuple(table.name for table in self.tables)
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("schema catalog contains duplicate table identifiers")
        if len(set(table_names)) != len(table_names):
            raise ValueError("schema catalog contains duplicate table names")
        if any(table.schema_name != self.name for table in self.tables):
            raise ValueError("table schema name does not match its parent schema")
        return self


class DatabaseCatalog(FrozenContract):
    """Fingerprint-bound snapshot структуры целевой БД без credentials."""

    schema_version: SchemaVersionStr = "1.0.0"
    dialect: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    target_policy_fingerprint: FingerprintStr
    producer: ProducerMetadata
    schemas: tuple[SchemaCatalog, ...] = ()
    database_name: IdentifierStr | None = None

    @model_validator(mode="after")
    def validate_catalog_graph(self) -> Self:
        schema_ids = tuple(schema.schema_id for schema in self.schemas)
        schema_names = tuple(schema.name for schema in self.schemas)
        if len(set(schema_ids)) != len(schema_ids):
            raise ValueError("database catalog contains duplicate schema identifiers")
        if len(set(schema_names)) != len(schema_names):
            raise ValueError("database catalog contains duplicate schema names")

        tables = tuple(table for schema in self.schemas for table in schema.tables)
        table_by_id = {table.table_id: table for table in tables}
        if len(table_by_id) != len(tables):
            raise ValueError("database catalog contains duplicate table identifiers")

        for table in tables:
            for foreign_key in table.foreign_keys:
                referenced_table = table_by_id.get(foreign_key.referenced_table_id)
                if referenced_table is None:
                    raise ValueError("foreign key references an unknown table")
                referenced_columns = {
                    column.column_id for column in referenced_table.columns
                }
                if not set(foreign_key.referenced_column_ids) <= referenced_columns:
                    raise ValueError("foreign key references an unknown target column")
        return self

    def has_column(self, table_id: str, column_id: str) -> bool:
        """Проверить catalog reference без обращения к БД.

        Args:
            table_id: Стабильный идентификатор таблицы.
            column_id: Стабильный идентификатор столбца этой таблицы.

        Returns:
            ``True``, если каталог содержит указанный столбец.
        """

        return any(
            table.table_id == table_id
            and any(column.column_id == column_id for column in table.columns)
            for schema in self.schemas
            for table in schema.tables
        )


class CatalogColumnRef(FrozenContract):
    """Стабильная ссылка на столбец конкретной таблицы каталога."""

    table_id: IdentifierStr
    column_id: IdentifierStr


class DatabaseInspectionRequest(FrozenContract):
    """Read-only запрос инспекции по непрозрачному target ID, без DSN."""

    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    read_only: Literal[True] = True
    schema_names: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def validate_schema_names(self) -> Self:
        if len(set(self.schema_names)) != len(self.schema_names):
            raise ValueError("inspection request contains duplicate schema names")
        return self


class MappingPolicyRef(FrozenContract):
    """Ссылка на versioned policy, применённую к DB mapping."""

    policy_id: IdentifierStr
    policy_fingerprint: FingerprintStr


class LoadPolicy(FrozenContract):
    """Явные safety guarantees, обязательные для исполнения MappingPlan."""

    dry_run: StrictBool
    error_policy: ErrorPolicy
    target_policy_fingerprint: FingerprintStr
    allowlist_ref: IdentifierStr
    denylist_ref: IdentifierStr | None
    staging_required: StrictBool
    transactional_audit_required: StrictBool
    rollback_required: StrictBool

    @model_validator(mode="after")
    def validate_write_safety(self) -> Self:
        if self.dry_run:
            return self
        if not self.staging_required:
            raise ValueError("non-dry-run load requires staging")
        if not self.transactional_audit_required:
            raise ValueError("non-dry-run load requires transactional audit")
        if not self.rollback_required:
            raise ValueError("non-dry-run load requires rollback")
        return self


class LoadContext(FrozenContract):
    """Fingerprint-bound execution context без connection handle."""

    run_id: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    policy: LoadPolicy
    idempotency_key: IdentifierStr
    deadline: UtcDateTime | None
    cancellation_token_id: IdentifierStr
    staging_capability_evidence: IdentifierStr
    audit_capability_evidence: IdentifierStr


class StagingContext(FrozenContract):
    """Контекст staging, связанный с конкретным normalized dataset и target."""

    run_id: IdentifierStr
    staging_id: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    target_policy_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    expires_at: UtcDateTime
    max_records: _PositiveInt
