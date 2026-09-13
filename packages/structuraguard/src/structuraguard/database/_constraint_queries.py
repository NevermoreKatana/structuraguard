"""Закрытый SQLAlchemy builder; SQL/identifiers не принимаются из expressions."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import NullScalar, StringScalar
from structuraguard.contracts.constraint_validation import (
    ConstraintLookup,
    ConstraintMatch,
    ConstraintReadPolicy,
    ConstraintReadRequest,
    ConstraintReadResult,
)
from structuraguard.contracts.database import DatabaseMetadataSnapshot, TableCatalog
from structuraguard.domain.constraint_semantics import unique_keys
from structuraguard.domain.constraint_values import field_codes
from structuraguard.domain.database_fingerprint import verify_database_fingerprint

from ._inspection import failure

if TYPE_CHECKING:
    from sqlalchemy import Select, Table
    from sqlalchemy.sql.elements import ColumnElement
    from sqlalchemy.types import TypeEngine


def checked_tables(
    snapshot: DatabaseMetadataSnapshot,
    request: ConstraintReadRequest,
    policy: ConstraintReadPolicy,
) -> dict[str, TableCatalog]:
    verify_database_fingerprint(snapshot, request.database_fingerprint)
    tables = {t.table_id: t for s in snapshot.schemas for t in s.tables}
    allow = {(c.table_id, c.column_id) for c in policy.allow_columns}
    for key in request.lookups:
        table = tables.get(key.table_id)
        if (
            table is None
            or table.inspection is None
            or table.inspection.kind != "table"
            or table.inspection.partitioned
        ):
            raise failure("DB_CONSTRAINT_UNVERIFIED")
        columns = {c.column_id: c for c in table.columns}
        for cid, value in zip(
            (*key.column_ids, *key.identity_column_ids),
            (*key.values, *key.identity_values),
            strict=True,
        ):
            if (key.table_id, cid) not in allow or cid not in columns:
                raise failure("TARGET_NOT_ALLOWED")
            if field_codes(columns[cid], value, snapshot.dialect):
                raise failure("DB_CONSTRAINT_UNVERIFIED")
        supported = unique_keys(table, snapshot.dialect)
        if not any(
            k.column_ids == key.column_ids
            and k.supported
            and (
                k.nulls_equal or not any(isinstance(v, NullScalar) for v in key.values)
            )
            for k in supported
        ):
            raise failure("DB_CONSTRAINT_UNVERIFIED")
        if key.identity_column_ids and (
            not any(
                k.column_ids == key.identity_column_ids and k.supported
                for k in supported
            )
            or any(isinstance(v, NullScalar) for v in key.identity_values)
        ):
            raise failure("DB_UPSERT_IDENTITY_INVALID")
    return tables


def statements(
    request: ConstraintReadRequest,
    tables: dict[str, TableCatalog],
    chunk_size: int,
    dialect: str,
) -> Iterator[tuple[tuple[ConstraintLookup, ...], Select[tuple[object, ...]]]]:
    from sqlalchemy import select

    for start in range(0, len(request.lookups), chunk_size):
        chunk = request.lookups[start : start + chunk_size]
        expressions: list[ColumnElement[bool]] = []
        for key in chunk:
            relation = _relation(
                tables[key.table_id], (*key.column_ids, *key.identity_column_ids)
            )
            predicate = _predicate(
                tables[key.table_id], relation, key.column_ids, key.values, dialect
            )
            present = select(1).where(predicate).exists()
            if key.identity_column_ids:
                other_identity = ~_predicate(
                    tables[key.table_id],
                    relation,
                    key.identity_column_ids,
                    key.identity_values,
                    dialect,
                )
                conflicts = select(1).where(predicate, other_identity).exists()
            else:
                conflicts = present
            expressions.extend((present, conflicts))
        yield chunk, select(*expressions)


def _relation(table: TableCatalog, column_ids: tuple[str, ...]) -> Table:
    from sqlalchemy import (
        BigInteger,
        Boolean,
        Column,
        Date,
        DateTime,
        Float,
        MetaData,
        Numeric,
        String,
        Table,
        Uuid,
    )

    columns = {c.column_id: c for c in table.columns}
    relation = Table(table.name, MetaData(), schema=table.schema_name)
    for cid in dict.fromkeys(column_ids):
        column = columns[cid]
        assert column.inspection is not None
        kind = column.inspection.data_type.canonical_type
        sql_types: dict[str, TypeEngine[Any]] = {
            "integer": BigInteger(),
            "decimal": Numeric(),
            "float": Float(precision=53),
            "boolean": Boolean(),
            "date": Date(),
            "datetime": DateTime(timezone=True),
            "uuid": Uuid(),
            "text": String(),
        }
        relation.append_column(Column(column.name, sql_types[kind]))
    return relation


def _predicate(
    table: TableCatalog,
    relation: Table,
    column_ids: tuple[str, ...],
    values: tuple[object, ...],
    dialect: str,
) -> ColumnElement[bool]:
    from sqlalchemy import and_

    from structuraguard.contracts.common import (
        BooleanScalar,
        DateScalar,
        DateTimeScalar,
        DecimalScalar,
        IntegerScalar,
    )

    columns = {c.column_id: c for c in table.columns}
    predicates: list[ColumnElement[bool]] = []
    for cid, scalar in zip(column_ids, values, strict=True):
        column = columns[cid]
        assert column.inspection is not None
        kind = column.inspection.data_type.canonical_type
        operand: ColumnElement[Any] = relation.c[column.name]
        if kind == "text":
            operand = operand.collate("BINARY" if dialect == "sqlite" else "C")
        if not isinstance(
            scalar,
            StringScalar
            | NullScalar
            | IntegerScalar
            | DecimalScalar
            | BooleanScalar
            | DateScalar
            | DateTimeScalar,
        ):
            raise failure("DB_CONSTRAINT_UNVERIFIED")
        value: object = scalar.value
        if kind == "uuid" and isinstance(scalar, StringScalar):
            value = UUID(scalar.value)
        predicates.append(operand.is_not_distinct_from(value))
    return and_(*predicates)


def matches(
    chunk: tuple[ConstraintLookup, ...], row: tuple[object, ...]
) -> tuple[ConstraintMatch, ...]:
    if len(row) != 2 * len(chunk):
        raise failure("DB_READER_RESULT_INVALID")
    if any(type(value) is not bool for value in row):
        raise failure("DB_READER_RESULT_INVALID")
    return tuple(
        ConstraintMatch(
            lookup_id=key.lookup_id,
            exists=bool(row[2 * i]),
            conflicts=bool(row[2 * i + 1]),
        )
        for i, key in enumerate(chunk)
    )


def result(
    request: ConstraintReadRequest, values: tuple[ConstraintMatch, ...]
) -> ConstraintReadResult:
    return ConstraintReadResult(
        request_fingerprint=request.fingerprint,
        snapshot_fingerprint=canonical_sha256_value(
            (request.fingerprint, tuple(m.canonical_json() for m in values))
        ),
        matches=values,
    )
