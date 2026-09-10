"""Versioned fingerprint структуры; opaque IDs и runtime binding исключены."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from structuraguard.contracts._base import CanonicalValue
from structuraguard.contracts.database import DatabaseType, TableCatalog
from structuraguard.exceptions import DatabaseInspectionError

from ._database_catalog import CatalogInput, validated_metadata
from .canonical import canonical_json


def _sorted(values: Iterable[CanonicalValue]) -> list[CanonicalValue]:
    return sorted(values, key=canonical_json)


def _type(data_type: DatabaseType) -> CanonicalValue:
    projection: dict[str, CanonicalValue] = {
        "native_type": data_type.native_type,
        "canonical_type": data_type.canonical_type,
        "length": data_type.length,
        "precision": data_type.precision,
        "scale": data_type.scale,
        "timezone": data_type.timezone,
        "affinity": data_type.affinity,
        "type_kind": data_type.type_kind,
        "enum_labels": data_type.enum_labels,
        "base_type": _type(data_type.base_type) if data_type.base_type else None,
        "element_type": _type(data_type.element_type)
        if data_type.element_type
        else None,
        "domain_checks": sorted(data_type.domain_checks),
        "domain_not_null": data_type.domain_not_null,
        "domain_default": data_type.domain_default,
    }
    if data_type.domain_constraints is not None:
        projection["domain_constraints"] = _sorted(
            {
                "name": item.name,
                "kind": item.kind,
                "expression": item.expression,
                "comment": item.comment,
                "deferrable": item.deferrable,
                "initially_deferred": item.initially_deferred,
                "validated": item.validated,
                "enforced": item.enforced,
                "no_inherit": item.no_inherit,
            }
            for item in data_type.domain_constraints
        )
    return projection


def _table(
    table: TableCatalog, tables: dict[str, TableCatalog], dialect: str
) -> CanonicalValue:
    metadata = table.inspection
    assert metadata is not None
    names = {column.column_id: column.name for column in table.columns}
    columns: list[CanonicalValue] = []
    for column in table.columns:
        info = column.inspection
        assert info is not None
        columns.append(
            {
                "name": column.name,
                "type": _type(info.data_type),
                "ordinal": info.ordinal_position,
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "unique": column.unique,
                "generated": column.generated,
                "comment": column.comment,
                "default": info.default,
                "generation_expression": info.generation_expression,
                "generation_storage": info.generation_storage,
                "identity": info.identity,
                "autoincrement": info.autoincrement,
                "rowid_alias": info.rowid_alias,
            }
        )
    keys: list[CanonicalValue] = []
    for key in table.foreign_keys:
        parent = tables[key.referenced_table_id]
        parent_names = {column.column_id: column.name for column in parent.columns}
        info_fk = key.inspection
        keys.append(
            {
                "columns": [names[item] for item in key.column_ids],
                "parent": [parent.schema_name, parent.name],
                "parent_columns": [
                    parent_names[item] for item in key.referenced_column_ids
                ],
                "inspection": None
                if info_fk is None
                else {
                    "name": info_fk.name,
                    "on_update": info_fk.on_update,
                    "on_delete": info_fk.on_delete,
                    "match": info_fk.match,
                    "deferrable": info_fk.deferrable,
                    "initially_deferred": info_fk.initially_deferred,
                    "on_delete_columns": [
                        names[item] for item in info_fk.on_delete_column_ids
                    ],
                },
            }
        )
    return {
        "name": table.name,
        "comment": table.comment,
        "kind": metadata.kind,
        "partitioned": metadata.partitioned,
        "strict": metadata.strict,
        "without_rowid": metadata.without_rowid,
        "columns": _sorted(columns),
        "primary_key": [names[item] for item in table.primary_key],
        "foreign_keys": _sorted(keys),
        "unique_constraints": _sorted(
            [names[item] for item in key] for key in table.unique_constraints
        ),
        "checks": sorted(table.check_constraints),
        "constraints": _sorted(
            {
                "name": item.name,
                "kind": item.kind,
                "columns": [names[col] for col in item.column_ids],
                "expression": item.expression,
                "comment": item.comment,
                "deferrable": item.deferrable,
                "initially_deferred": item.initially_deferred,
                "validated": item.validated,
                "enforced": item.enforced,
                "no_inherit": item.no_inherit,
            }
            for item in metadata.constraints
        ),
        "indexes": _sorted(
            {
                # SQLite назначает autoindex suffix по порядку объявления constraints.
                "name": None
                if dialect == "sqlite" and item.origin != "index"
                else item.name,
                "keys": [
                    {
                        "column": names[key.column_id] if key.column_id else None,
                        "expression": key.expression,
                        "descending": key.descending,
                        "collation": key.collation,
                        "nulls_first": key.nulls_first,
                        "operator_class": key.operator_class,
                    }
                    for key in item.keys
                ],
                "unique": item.unique,
                "predicate": item.predicate,
                "origin": item.origin,
                "include_columns": [names[col] for col in item.include_column_ids],
                "method": item.method,
                "nulls_not_distinct": item.nulls_not_distinct,
                "valid": item.valid,
                "comment": item.comment,
            }
            for item in metadata.indexes
        ),
    }


def canonical_database_catalog(catalog: CatalogInput) -> str:
    """Вернуть catalog-v1 JSON с явным набором структурных полей.

    Коллекции сортируются по содержимому; порядок composite keys, enum labels
    и ordinal columns сохраняется. SQL expressions сравниваются буквально,
    эквивалентность SQL или схем разных диалектов не предполагается.

    Args:
        catalog: Snapshot schema 1.1.0 или DatabaseCatalog формата catalog-v1.
            Metadata повторно валидируются; legacy catalog не поддержан.

    Returns:
        Канонический JSON структуры без opaque IDs, target/policy binding,
        producer, permissions и derived graph. Представимые comments включены.

    Raises:
        DatabaseInspectionError: ``DATABASE_METADATA_UNSUPPORTED`` для legacy
            или неподдержанной версии каталога.
        pydantic.ValidationError: Нарушение metadata invariants.

    Side effects:
        Нет I/O или исполнения SQL. Caller ограничивает размер входа; pure
        функция не задаёт deadline. Результат может содержать sensitive metadata.
    """
    snapshot = validated_metadata(catalog)
    tables = {
        table.table_id: table for schema in snapshot.schemas for table in schema.tables
    }
    return canonical_json(
        {
            "format": "catalog-v1",
            "dialect": snapshot.dialect,
            "comments_supported": snapshot.comments_supported,
            "schemas": _sorted(
                {
                    "name": schema.name,
                    "comment": schema.comment,
                    "tables": _sorted(
                        _table(table, tables, snapshot.dialect)
                        for table in schema.tables
                    ),
                }
                for schema in snapshot.schemas
            ),
        }
    )


def database_fingerprint(catalog: CatalogInput) -> str:
    """Вычислить SHA-256 canonical UTF-8 catalog без обращения к БД.

    Args:
        catalog: Snapshot schema 1.1.0 или DatabaseCatalog формата catalog-v1.

    Returns:
        Строка ``sha256:`` и 64 lowercase hex-символа по явной projection.

    Raises:
        DatabaseInspectionError: ``DATABASE_METADATA_UNSUPPORTED`` для
            неподдержанной версии, как у ``canonical_database_catalog``.
        pydantic.ValidationError: Нарушение metadata invariants.

    Side effects:
        Нет I/O; ограничения размера входа задаёт caller. Hash не удостоверяет
        автора/target/права, не покрывает view SQL и не заменяет новый inspection.
    """
    return (
        "sha256:"
        + hashlib.sha256(
            canonical_database_catalog(catalog).encode("utf-8")
        ).hexdigest()
    )


def verify_database_fingerprint(catalog: CatalogInput, expected: str) -> None:
    """Сравнить fingerprint переданного каталога с ожидаемым значением.

    Args:
        catalog: Новый snapshot schema 1.1.0 или DatabaseCatalog catalog-v1.
        expected: Ожидаемый schema fingerprint, например из ранее сохранённого
            плана. Любое несовпадение означает drift.

    Returns:
        None при совпадении пересчитанного fingerprint.

    Raises:
        DatabaseInspectionError: ``DATABASE_SCHEMA_DRIFT`` при несовпадении
            либо ``DATABASE_METADATA_UNSUPPORTED`` для неподдержанной версии.
        pydantic.ValidationError: Нарушение metadata invariants.

    Side effects:
        Нет I/O: сама функция не подключается к БД. Caller получает свежий
        catalog, ограничивает его размер и проверяет target/policy и TOCTOU.
        Успех сравнения не разрешает загрузку.
    """
    if database_fingerprint(catalog) != expected:
        raise DatabaseInspectionError(
            error_code="DATABASE_SCHEMA_DRIFT",
            message="Структура каталога изменилась; требуется повторная валидация плана.",
        )
