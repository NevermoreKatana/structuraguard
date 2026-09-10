"""Bounded reflection только разрешённых PostgreSQL objects и их типов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from structuraguard.contracts.database import (
    ColumnCatalog,
    ColumnInspectionMetadata,
    ConstraintInspectionMetadata,
    DatabaseMetadataSnapshot,
    DatabaseType,
    ForeignKeyCatalog,
    ForeignKeyInspectionMetadata,
    IndexCatalog,
    IndexKeyCatalog,
    SchemaCatalog,
    TableCatalog,
    TableInspectionMetadata,
)

from . import _postgresql_queries as queries
from ._inspection import failure
from .normalization import catalog_identifier, normalize_type
from .target import PostgreSQLTarget

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection

type Row = dict[str, object]
type TableName = tuple[str, str]


def string(row: Row, key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise failure("DATABASE_METADATA_UNSUPPORTED")
    return value


def optional_string(row: Row, key: str) -> str | None:
    return None if row[key] is None else string(row, key)


def integer(row: Row, key: str) -> int:
    value = row[key]
    if type(value) is not int:
        raise failure("DATABASE_METADATA_UNSUPPORTED")
    return value


def flag(row: Row, key: str) -> bool:
    value = row[key]
    if type(value) is not bool:
        raise failure("DATABASE_METADATA_UNSUPPORTED")
    return value


def positions(row: Row, key: str) -> tuple[int, ...]:
    value = row[key]
    if value is None:
        return ()
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        raise failure("DATABASE_METADATA_UNSUPPORTED")
    return tuple(cast(list[int], value))


class PostgreSQLReader:
    """Поток metadata с row/text/byte limits; DTO получает только полные значения."""

    def __init__(
        self,
        connection: AsyncConnection,
        target: PostgreSQLTarget,
        schemas: frozenset[str],
    ) -> None:
        self.connection = connection
        self.target = target
        self.schemas = schemas
        self.items = 0
        self.bytes = 0
        self.types: dict[tuple[int, int], DatabaseType] = {}
        self.server_version = 0

    def account(self, value: object) -> None:
        if isinstance(value, str):
            if len(value) > self.target.limits.max_text_chars:
                raise failure("SECURITY_LIMIT_EXCEEDED")
            self.bytes += len(value.encode("utf-8"))
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.account(item)
        else:
            self.bytes += 8
        if self.bytes > self.target.limits.max_metadata_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")

    async def rows(
        self, query: str, parameters: Mapping[str, object], limit: int
    ) -> list[Row]:
        from sqlalchemy import text

        limits = self.target.limits
        if len(query.encode("utf-8")) > limits.max_sql_bytes:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        bound = min(limit, limits.max_items - self.items)
        if bound < 0:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        values = dict(parameters)
        values.update(row_limit=bound + 1, text_limit=limits.max_text_chars + 1)
        result: list[Row] = []
        async with self.connection.stream(
            text(query), values, execution_options={"yield_per": 1}
        ) as cursor:
            async for record in cursor.mappings():
                self.items += 1
                if len(result) >= bound:
                    raise failure("SECURITY_LIMIT_EXCEEDED")
                row = dict(cast(Mapping[str, object], record))
                for value in row.values():
                    self.account(value)
                result.append(row)
        return result

    async def data_type(
        self, oid: int, modifier: int, *, depth: int = 0
    ) -> DatabaseType:
        if depth >= 8:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        key = (oid, modifier)
        if key in self.types:
            return self.types[key]
        schemas = sorted({"pg_catalog"} | self.schemas)
        rows = await self.rows(
            queries.TYPE, {"oid": oid, "modifier": modifier, "schemas": schemas}, 1
        )
        if not rows:
            raise failure("DATABASE_CATALOG_SCOPE_INCOMPLETE")
        row = rows[0]
        schema = string(row, "schema")
        if schema != "pg_catalog" and schema not in self.schemas:
            raise failure("DATABASE_CATALOG_SCOPE_INCOMPLETE")
        native = string(row, "native_type")
        kind = string(row, "kind")
        common: dict[str, object] = {"native_type": native, "canonical_type": "unknown"}
        if kind == "e":
            labels = await self.rows(
                queries.ENUM, {"oid": oid}, self.target.limits.max_constraints
            )
            common.update(
                type_kind="enum", enum_labels=tuple(string(r, "label") for r in labels)
            )
        elif kind == "d":
            base = await self.data_type(
                integer(row, "base_oid"), integer(row, "modifier"), depth=depth + 1
            )
            checks = await self.rows(
                queries.DOMAIN_CHECKS, {"oid": oid}, self.target.limits.max_constraints
            )
            constraints: list[ConstraintInspectionMetadata] = []
            for check in checks:
                name = string(check, "name")
                if name != name.strip():
                    raise failure("DATABASE_METADATA_UNSUPPORTED")
                constraints.append(
                    ConstraintInspectionMetadata(
                        name=name,
                        kind="check",
                        expression=string(check, "expression"),
                        comment=optional_string(check, "comment"),
                        validated=flag(check, "validated"),
                    )
                )
            common.update(
                type_kind="domain",
                base_type=base,
                canonical_type=base.canonical_type,
                domain_checks=tuple(sorted(string(r, "expression") for r in checks)),
                domain_constraints=tuple(sorted(constraints, key=lambda c: c.name)),
                domain_not_null=flag(row, "not_null") or base.domain_not_null,
                domain_default=optional_string(row, "default")
                if row["default"] is not None
                else base.domain_default,
            )
        elif string(row, "category") == "A" and integer(row, "element_oid"):
            common.update(
                type_kind="array",
                element_type=await self.data_type(
                    integer(row, "element_oid"), modifier, depth=depth + 1
                ),
            )
        elif schema == "pg_catalog" and kind == "b":
            common = normalize_type(native).model_dump()
            common["type_kind"] = "builtin"
        else:
            common["type_kind"] = "unknown"
        result = DatabaseType.model_validate(common)
        self.types[key] = result
        return result


@dataclass
class ReflectedTable:
    schema: str
    name: str
    metadata: Row
    columns: list[Row]
    constraints: list[Row]

    def column_id(self, position: int) -> str:
        for column in self.columns:
            if integer(column, "position") == position:
                return catalog_identifier(
                    "column", self.schema, self.name, string(column, "name")
                )
        raise failure("DATABASE_METADATA_UNSUPPORTED")


async def reflect(
    reader: PostgreSQLReader, names: tuple[TableName, ...]
) -> DatabaseMetadataSnapshot:
    target = reader.target
    schemas: dict[str, Row] = {}
    tables: dict[TableName, ReflectedTable] = {}
    for schema in sorted({name[0] for name in names}):
        rows = await reader.rows(queries.SCHEMA, {"schema": schema}, 1)
        if not rows:
            raise failure("DATABASE_OBJECT_NOT_FOUND")
        if not flag(rows[0], "usable"):
            raise failure("DATABASE_PERMISSION_DENIED")
        schemas[schema] = rows[0]
        comment = await reader.rows(
            queries.SCHEMA_COMMENT, {"oid": integer(rows[0], "oid")}, 1
        )
        schemas[schema]["comment"] = comment[0]["comment"]
    for schema, name in names:
        rows = await reader.rows(
            queries.RELATION,
            {"schema_oid": integer(schemas[schema], "oid"), "table": name},
            1,
        )
        if not rows:
            raise failure("DATABASE_OBJECT_NOT_FOUND")
        metadata = rows[0]
        if string(metadata, "kind") not in {"r", "p", "v", "m"}:
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        oid = integer(metadata, "oid")
        constraints = await reader.rows(
            queries.CONSTRAINTS_18
            if reader.server_version >= 180000
            else queries.CONSTRAINTS,
            {"oid": oid},
            target.limits.max_constraints,
        )
        for constraint in constraints:
            if (
                string(constraint, "kind") == "f"
                and (
                    string(constraint, "foreign_schema"),
                    string(constraint, "foreign_table"),
                )
                not in names
            ):
                raise failure("DATABASE_CATALOG_SCOPE_INCOMPLETE")
        columns = await reader.rows(
            queries.COLUMNS, {"oid": oid}, target.limits.max_columns
        )
        # DTO хранит index collation, но не column/domain collation: не скрывать drift.
        if any(not flag(column, "default_collation") for column in columns):
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        tables[(schema, name)] = ReflectedTable(
            schema, name, metadata, columns, constraints
        )
    catalog: dict[str, list[TableCatalog]] = {name: [] for name in schemas}
    for table in tables.values():
        catalog[table.schema].append(await _table(reader, table, tables))
    return DatabaseMetadataSnapshot(
        dialect="postgresql",
        target_id=target.target_id,
        target_policy_fingerprint=target.policy_fingerprint,
        comments_supported=True,
        schemas=tuple(
            SchemaCatalog(
                schema_id=catalog_identifier("schema", schema),
                name=schema,
                comment=optional_string(schemas[schema], "comment"),
                tables=tuple(sorted(catalog[schema], key=lambda t: t.name)),
            )
            for schema in sorted(schemas)
        ),
    )


async def _table(
    reader: PostgreSQLReader,
    table: ReflectedTable,
    tables: dict[TableName, ReflectedTable],
) -> TableCatalog:
    pk: tuple[str, ...] = ()
    unique: set[tuple[str, ...]] = set()
    checks: list[str] = []
    foreign: list[ForeignKeyCatalog] = []
    constraints: list[ConstraintInspectionMetadata] = []
    kinds: dict[
        str, Literal["primary_key", "unique", "check", "foreign_key", "not_null"]
    ] = {
        "p": "primary_key",
        "u": "unique",
        "c": "check",
        "f": "foreign_key",
        "n": "not_null",
    }
    for row in table.constraints:
        kind = string(row, "kind")
        if kind not in kinds or flag(row, "period"):
            # EXCLUDE нельзя выдавать за обычный unique/check constraint.
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        columns = tuple(table.column_id(p) for p in positions(row, "columns"))
        constraints.append(
            ConstraintInspectionMetadata(
                name=string(row, "name"),
                kind=kinds[kind],
                column_ids=columns,
                expression=optional_string(row, "expression"),
                comment=optional_string(row, "comment"),
                deferrable=flag(row, "deferrable"),
                initially_deferred=flag(row, "initially_deferred"),
                validated=flag(row, "validated"),
                enforced=flag(row, "enforced"),
                no_inherit=flag(row, "no_inherit"),
            )
        )
        if kind == "p":
            pk = columns
        elif kind == "u":
            unique.add(columns)
        elif kind == "c":
            checks.append(string(row, "expression"))
        elif kind == "f":
            remote = tables[
                (string(row, "foreign_schema"), string(row, "foreign_table"))
            ]
            foreign.append(
                ForeignKeyCatalog(
                    foreign_key_id=catalog_identifier(
                        "foreign_key", table.schema, table.name, string(row, "name")
                    ),
                    column_ids=columns,
                    referenced_table_id=catalog_identifier(
                        "table", remote.schema, remote.name
                    ),
                    referenced_column_ids=tuple(
                        remote.column_id(p) for p in positions(row, "foreign_columns")
                    ),
                    inspection=ForeignKeyInspectionMetadata.model_validate(
                        {
                            "name": string(row, "name"),
                            "on_update": _action(string(row, "on_update")),
                            "on_delete": _action(string(row, "on_delete")),
                            "match": {"s": "SIMPLE", "f": "FULL", "p": "PARTIAL"}[
                                string(row, "match")
                            ],
                            "deferrable": flag(row, "deferrable"),
                            "on_delete_column_ids": tuple(
                                table.column_id(p)
                                for p in positions(row, "delete_columns")
                            ),
                            "initially_deferred": flag(row, "initially_deferred"),
                        }
                    ),
                )
            )
    kind = string(table.metadata, "kind")
    writable = kind in {"r", "p"}
    columns_out: list[ColumnCatalog] = []
    # attnum нужен для PK/FK, но пропуски от DROP COLUMN не меняют логическую схему.
    for ordinal, row in enumerate(
        sorted(table.columns, key=lambda c: integer(c, "position"))
    ):
        data_type = await reader.data_type(
            integer(row, "type_oid"), integer(row, "type_modifier")
        )
        # Повторное использование enum/domain cache не обходит размер snapshot.
        reader.bytes += len(data_type.model_dump_json().encode("utf-8"))
        reader.account(None)
        column_id = table.column_id(integer(row, "position"))
        generation = string(row, "generated")
        identity = string(row, "identity")
        if generation not in {"", "s", "v"} or identity not in {"", "a", "d"}:
            raise failure("DATABASE_METADATA_UNSUPPORTED")
        expression = optional_string(row, "expression")
        metadata = ColumnInspectionMetadata.model_validate(
            {
                "data_type": data_type,
                "ordinal_position": ordinal,
                "default": None
                if generation
                else expression
                if expression is not None
                else data_type.domain_default,
                "generation_expression": expression if generation else None,
                "generation_storage": {"": None, "s": "stored", "v": "virtual"}[
                    generation
                ],
                "identity": {"": None, "a": "always", "d": "by_default"}[identity],
            }
        )
        columns_out.append(
            ColumnCatalog(
                column_id=column_id,
                name=string(row, "name"),
                type_name=data_type.canonical_type,
                nullable=not flag(row, "not_null") and not data_type.domain_not_null,
                primary_key=column_id in pk,
                unique=(column_id,) in unique,
                generated=bool(generation),
                writable=writable and not generation and identity != "a",
                comment=optional_string(row, "comment"),
                inspection=metadata,
            )
        )
    return TableCatalog(
        table_id=catalog_identifier("table", table.schema, table.name),
        schema_name=table.schema,
        name=table.name,
        columns=tuple(sorted(columns_out, key=lambda c: c.name)),
        primary_key=pk,
        foreign_keys=tuple(sorted(foreign, key=lambda f: f.foreign_key_id)),
        unique_constraints=tuple(sorted(unique)),
        check_constraints=tuple(sorted(checks)),
        writable=writable,
        comment=optional_string(table.metadata, "comment"),
        inspection=TableInspectionMetadata(
            kind="view"
            if kind == "v"
            else "materialized_view"
            if kind == "m"
            else "table",
            partitioned=kind == "p",
            indexes=await _indexes(reader, table),
            constraints=tuple(sorted(constraints, key=lambda c: c.name)),
        ),
    )


def _action(code: str) -> str:
    return {
        "a": "NO ACTION",
        "r": "RESTRICT",
        "c": "CASCADE",
        "n": "SET NULL",
        "d": "SET DEFAULT",
    }[code]


async def _indexes(
    reader: PostgreSQLReader, table: ReflectedTable
) -> tuple[IndexCatalog, ...]:
    rows = await reader.rows(
        queries.INDEXES,
        {"oid": integer(table.metadata, "oid")},
        reader.target.limits.max_constraints,
    )
    indexes: list[IndexCatalog] = []
    for row in rows:
        keys = await reader.rows(
            queries.INDEX_KEYS,
            {"oid": integer(row, "oid")},
            reader.target.limits.max_columns,
        )
        key_count = integer(row, "key_count")
        index_keys: list[IndexKeyCatalog] = []
        included: list[str] = []
        for key in keys:
            position = integer(key, "column_position")
            if integer(key, "position") >= key_count:
                included.append(table.column_id(position))
                continue
            index_keys.append(
                IndexKeyCatalog(
                    column_id=table.column_id(position) if position else None,
                    expression=None if position else string(key, "expression"),
                    descending=flag(key, "descending"),
                    nulls_first=flag(key, "nulls_first"),
                    collation=optional_string(key, "collation"),
                    operator_class=optional_string(key, "operator_class"),
                )
            )
        indexes.append(
            IndexCatalog(
                index_id=catalog_identifier(
                    "index", table.schema, table.name, string(row, "name")
                ),
                name=string(row, "name"),
                keys=tuple(index_keys),
                unique=flag(row, "unique"),
                predicate=optional_string(row, "predicate"),
                origin="primary_key"
                if flag(row, "primary")
                else "unique_constraint"
                if flag(row, "constraint")
                else "index",
                include_column_ids=tuple(included),
                method=string(row, "method"),
                nulls_not_distinct=flag(row, "nulls_not_distinct"),
                valid=flag(row, "valid"),
                comment=optional_string(row, "comment"),
            )
        )
    return tuple(sorted(indexes, key=lambda index: index.name))
