"""Закрытый bulk INSERT/UPSERT builder; identifiers только из связанного catalog."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import LoadOperation, StringScalar
from structuraguard.contracts.database import DatabaseCatalog, TableCatalog
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    ExecutionStep,
    PostgreSQLLoadPolicy,
)
from structuraguard.domain.constraint_semantics import unique_keys
from structuraguard.loading.projection import Prepared, failure

from ._constraint_queries import _relation

if TYPE_CHECKING:
    from sqlalchemy.sql.dml import ReturningInsert


@dataclass(frozen=True, repr=False)
class WriteBatch:
    unit_ids: tuple[str, ...]
    statement: ReturningInsert[tuple[int]]


def statements(
    prepared: Prepared,
    plan: DryRunExecutionPlan,
    catalog: DatabaseCatalog,
    policy: PostgreSQLLoadPolicy,
    *,
    unit_ids: frozenset[str] | None = None,
) -> Iterator[WriteBatch]:
    """Разбить полный план на bounded groups с одинаковым table/column shape."""
    from sqlalchemy import literal
    from sqlalchemy.dialects.postgresql import insert

    if (
        not plan.ready
        or (
            plan.planned_quarantine
            and policy.preflight.error_policy != "quarantine_invalid"
        )
        or plan.mapping_fingerprint != prepared.request.mapping.fingerprint
        or plan.database_fingerprint != catalog.database_fingerprint
        or plan.projection_fingerprint
        != canonical_sha256_value(prepared.data.canonical_json())
    ):
        raise failure("LOAD_PLAN_INVALID")
    rows = {r.record_id: r for r in prepared.data.records}
    if set(rows) != {s.unit_id for s in plan.steps} or len(rows) != len(plan.steps):
        raise failure("LOAD_PLAN_INVALID")
    if unit_ids is not None and not unit_ids <= rows.keys():
        raise failure("LOAD_PLAN_INVALID")
    tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
    pending: list[dict[str, object]] = []
    ids: list[str] = []
    first: ExecutionStep | None = None
    byte_count = 0

    def emit(step: ExecutionStep) -> WriteBatch:
        table = tables[step.table_id]
        names = {c.column_id: c.name for c in table.columns}
        relation = _relation(table, step.column_ids)
        statement = insert(relation).values(pending)
        if (
            prepared.request.mapping.operation is LoadOperation.UPSERT
            and step.update_column_ids
        ):
            statement = statement.on_conflict_do_update(
                index_elements=[
                    relation.c[names[cid]] for cid in step.identity_column_ids
                ],
                set_={
                    relation.c[names[cid]]: statement.excluded[names[cid]]
                    for cid in step.update_column_ids
                },
            )
        return WriteBatch(tuple(ids), statement.returning(literal(1)))

    for step in plan.steps:
        if unit_ids is not None and step.unit_id not in unit_ids:
            continue
        row = rows[step.unit_id]
        table = tables.get(step.table_id)
        if (
            table is None
            or row.collection_id != step.table_id
            or tuple(c.field_id for c in row.values) != step.column_ids
        ):
            raise failure("LOAD_PLAN_INVALID")
        if step.action == "quarantine":
            continue
        _columns(table, step, prepared.request.mapping.operation)
        if step.action == "skip":
            continue
        if step.action not in ("insert", "update"):
            raise failure("LOAD_PLAN_INVALID")
        size = len(row.canonical_json().encode("utf-8"))
        if (
            size > policy.max_batch_bytes
            or len(step.column_ids) + 1 > policy.max_parameters
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if first is not None and (
            step.table_id != first.table_id
            or step.column_ids != first.column_ids
            or step.identity_column_ids != first.identity_column_ids
            or step.update_column_ids != first.update_column_ids
            or len(ids) >= policy.batch_size
            or (len(ids) + 1) * len(step.column_ids) + 1 > policy.max_parameters
            or byte_count + size > policy.max_batch_bytes
        ):
            yield emit(first)
            pending, ids, byte_count = [], [], 0
            first = None
        columns = {c.column_id: c for c in table.columns}
        values: dict[str, object] = {}
        for cell in row.values:
            column = columns[cell.field_id]
            scalar = cell.value
            value: object = scalar.value
            if (
                column.inspection is not None
                and column.inspection.data_type.canonical_type == "uuid"
                and isinstance(scalar, StringScalar)
            ):
                value = UUID(scalar.value)
            values[column.name] = value
        first = step if first is None else first
        pending.append(values)
        ids.append(step.unit_id)
        byte_count += size
    if first is not None:
        yield emit(first)


def _columns(
    table: TableCatalog, step: ExecutionStep, operation: LoadOperation
) -> None:
    columns = {c.column_id: c for c in table.columns}
    if not step.column_ids or any(
        cid not in columns or columns[cid].generated or not columns[cid].writable
        for cid in step.column_ids
    ):
        raise failure("LOAD_COLUMN_NOT_WRITABLE")
    if operation is LoadOperation.UPSERT:
        if not any(
            k.supported and k.column_ids == step.identity_column_ids
            for k in unique_keys(table, "postgresql")
        ):
            raise failure("LOAD_UPSERT_IDENTITY_INVALID")
        mutable = tuple(
            cid
            for cid in step.column_ids
            if cid not in step.identity_column_ids and cid not in table.primary_key
        )
        if mutable != step.update_column_ids or (
            step.action == "update" and not mutable
        ):
            raise failure("LOAD_PLAN_INVALID")
