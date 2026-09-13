"""Одна outer transaction: target, quarantine, audit и idempotency marker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.database import DatabaseCatalog, DatabaseInspectionRequest
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    LoadRequest,
    PostgreSQLLoadPolicy,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.staging import StagingRun, StagingRunStatus
from structuraguard.exceptions import StructuraGuardError
from structuraguard.loading.groups import dependency_groups
from structuraguard.loading.planning import build_plan
from structuraguard.loading.projection import Prepared, failure
from structuraguard.loading.server_values import allowed_server_values

from . import _postgresql_queries as queries
from ._catalog import inspect_catalog
from ._dry_run_permissions import permissions, principal
from ._dry_run_reader import SnapshotReader
from ._load_ledger import Ledger
from ._load_queries import statements
from ._postgresql_catalog import PostgreSQLReader, integer, reflect, string
from .postgresql import (
    PostgreSQLDatabaseAdapter,
    _cleanup,
    _DriverConnection,
    _error_code,
)
from .target import PostgreSQLTarget
from .writer_target import writer_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


@dataclass(frozen=True)
class Outcome:
    status: StagingRunStatus
    plan: DryRunExecutionPlan | None
    code: str | None
    cancelled: bool
    warnings: tuple[str, ...] = ()
    result: PostgreSQLLoadResult | None = None


def sql_error(error: BaseException) -> str:
    current: object = error
    for _ in range(3):
        state = getattr(current, "sqlstate", None)
        if state in {"23505", "23503", "23514", "23502"}:
            return {
                "23505": "LOAD_DUPLICATE_KEY",
                "23503": "LOAD_FOREIGN_KEY",
                "23514": "LOAD_CHECK",
                "23502": "LOAD_NOT_NULL",
            }[state]
        current = getattr(current, "orig", None)
        if current is None:
            break
    return {
        "DATABASE_PERMISSION_DENIED": "LOAD_PERMISSION_DENIED",
        "PROCESSING_TIMEOUT": "PROCESSING_TIMEOUT",
    }.get(_error_code(error), "LOAD_DATABASE_FAILED")


async def _catalog(
    connection: AsyncConnection,
    target: PostgreSQLTarget,
    names: tuple[tuple[str, str], ...],
    schemas: frozenset[str],
    version: int,
    expected: str,
) -> DatabaseCatalog:
    reader = PostgreSQLReader(connection, target, schemas)
    reader.server_version = version
    catalog = await inspect_catalog(lambda: reflect(reader, names), target.limits)
    if catalog.database_fingerprint != expected:
        raise failure("DATABASE_SCHEMA_DRIFT")
    return catalog


async def _write(
    connection: AsyncConnection,
    target: PostgreSQLTarget,
    prepared: Prepared,
    plan: DryRunExecutionPlan,
    catalog: DatabaseCatalog,
    policy: PostgreSQLLoadPolicy,
    check_deadline: Callable[[], None],
) -> DryRunExecutionPlan:
    from sqlalchemy.exc import SQLAlchemyError

    async def group_write(units: frozenset[str] | None) -> None:
        written = 0
        batches = (
            statements(prepared, plan, catalog, policy)
            if units is None
            else statements(prepared, plan, catalog, policy, unit_ids=units)
        )
        for batch in batches:
            check_deadline()
            if (
                len(str(batch.statement.compile(dialect=connection.dialect)).encode())
                > target.limits.max_sql_bytes
            ):
                raise failure("SECURITY_LIMIT_EXCEEDED")
            cursor = await connection.execute(batch.statement)
            returned = cursor.all()
            cursor.close()
            if len(returned) != len(batch.unit_ids) or any(
                tuple(row) != (1,) for row in returned
            ):
                raise failure("LOAD_ROW_COUNT_MISMATCH")
            written += len(returned)
        expected = sum(
            s.action in ("insert", "update")
            for s in plan.steps
            if units is None or s.unit_id in units
        )
        if written != expected:
            raise failure("LOAD_ROW_COUNT_MISMATCH")

    if policy.preflight.error_policy == "atomic":
        await group_write(None)
        return plan
    groups = dependency_groups(
        prepared, plan, policy.preflight.read_policy.limits.max_evaluations
    )
    if (
        len(groups) * len(plan.steps)
        > policy.preflight.read_policy.limits.max_evaluations
    ):
        raise failure("SECURITY_LIMIT_EXCEEDED")
    for group in groups:
        units = frozenset(group)
        if any(s.action == "quarantine" for s in plan.steps if s.unit_id in units):
            if any(s.action != "quarantine" for s in plan.steps if s.unit_id in units):
                raise failure("LOAD_GROUP_UNRESOLVED")
            continue
        code: str | None = None
        try:
            async with connection.begin_nested():
                await group_write(units)
        except SQLAlchemyError as error:
            code = sql_error(error)
            if code not in {
                "LOAD_DUPLICATE_KEY",
                "LOAD_FOREIGN_KEY",
                "LOAD_CHECK",
                "LOAD_NOT_NULL",
            }:
                raise failure(code) from None
        if code is not None:
            plan = plan.model_copy(
                update={
                    "steps": tuple(
                        s.model_copy(
                            update={
                                "action": "quarantine",
                                "codes": tuple(sorted({*s.codes, code})),
                            }
                        )
                        if s.unit_id in units
                        else s
                        for s in plan.steps
                    )
                }
            )
    return plan


async def execute_transaction(
    target: PostgreSQLTarget,
    prepared: Prepared,
    policy: PostgreSQLLoadPolicy,
    *,
    request: LoadRequest,
    started_at: datetime,
    claim_staging: Callable[[], Awaitable[StagingRun]],
    check_deadline: Callable[[], None],
    timeout_seconds: float,
) -> Outcome:
    try:
        from asyncpg.exceptions import PostgresError
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError:
        return Outcome(
            StagingRunStatus.FAILED, None, "DATABASE_DEPENDENCY_UNAVAILABLE", False
        )
    engine: AsyncEngine | None = None
    connection: AsyncConnection | None = None
    driver: _DriverConnection | None = None
    plan: DryRunExecutionPlan | None = None
    receipt: PostgreSQLLoadResult | None = None
    commit_started = committed = cancelled = False
    code: str | None = None
    warnings: tuple[str, ...] = ()
    try:
        async with asyncio.timeout(timeout_seconds):
            mapping = prepared.request.mapping
            ledger = Ledger(request, policy) if policy.ledger is not None else None
            scope = DatabaseInspectionRequest(
                target_id=mapping.target_id,
                target_policy_fingerprint=mapping.target_policy_fingerprint,
            )
            names, schemas = PostgreSQLDatabaseAdapter(target)._scope(scope)
            if any(
                schema == "structuraguard_staging" or schema.startswith("sg_staging_")
                for schema, _ in names
            ):
                raise failure("TARGET_NOT_ALLOWED")
            engine = writer_engine(target)
            connection = await engine.connect()
            raw = await connection.get_raw_connection()
            driver = cast(_DriverConnection, raw.driver_connection)
            await connection.begin()
            state = (
                await PostgreSQLReader(connection, target, schemas).rows(
                    queries.STATE, {}, 1
                )
            )[0]
            if (
                string(state, "read_only") != "off"
                or string(state, "isolation") != "read committed"
            ):
                raise failure("LOAD_TRANSACTION_REQUIRED")
            version = integer(state, "version")
            if not 150000 <= version < 190000:
                raise failure("DATABASE_METADATA_UNSUPPORTED")
            if ledger is not None:
                await principal(
                    connection, policy.preflight.writer_principal, session_mode="writer"
                )
                receipt = await ledger.claim(connection)
            if receipt is not None:
                # Marker доказывает прошлый COMMIT; текущая transaction ничего не пишет.
                committed = True
            else:
                check_deadline()
                run = await claim_staging()
                quote = connection.dialect.identifier_preparer.quote_identifier
                for schema, name in names:
                    mode = (
                        "SHARE ROW EXCLUSIVE"
                        if (schema, name) in policy.write_tables
                        else "ACCESS SHARE"
                    )
                    sql = (
                        f"LOCK TABLE ONLY {quote(schema)}.{quote(name)} IN {mode} MODE"
                    )
                    if len(sql.encode()) > target.limits.max_sql_bytes:
                        raise failure("SECURITY_LIMIT_EXCEEDED")
                    await connection.exec_driver_sql(sql)
                catalog = await _catalog(
                    connection,
                    target,
                    names,
                    schemas,
                    version,
                    mapping.database_fingerprint,
                )
                selected = {m.target.table_id for m in mapping.mappings}
                if any(
                    (t.schema_name, t.name) not in policy.write_tables
                    for s in catalog.schemas
                    for t in s.tables
                    if t.table_id in selected
                ):
                    raise failure("TARGET_NOT_ALLOWED")
                await permissions(
                    connection,
                    catalog,
                    policy.preflight,
                    mapping,
                    session_mode="writer",
                )
                plan = await build_plan(
                    prepared,
                    catalog=catalog,
                    policy=policy.preflight,
                    reader=SnapshotReader(connection, policy.preflight.read_policy),
                    server_values=allowed_server_values(catalog, policy.server_values),
                )
                if not plan.ready or (
                    plan.planned_quarantine
                    and policy.preflight.error_policy == "atomic"
                ):
                    if any("DB_UNIQUE" in s.codes for s in plan.steps):
                        raise failure("LOAD_DUPLICATE_KEY")
                    if any("DB_FOREIGN_KEY" in s.codes for s in plan.steps):
                        raise failure("LOAD_FOREIGN_KEY")
                    raise failure("LOAD_VALIDATION_REJECTED")
                await _catalog(
                    connection,
                    target,
                    names,
                    schemas,
                    version,
                    mapping.database_fingerprint,
                )
                plan = await _write(
                    connection, target, prepared, plan, catalog, policy, check_deadline
                )
                rejected = {s.record_id for s in plan.steps if s.action == "quarantine"}
                assert run.sealed_fingerprint is not None
                receipt = PostgreSQLLoadResult(
                    run_id=request.staging_context.run_id,
                    target_id=target.target_id,
                    database_fingerprint=plan.database_fingerprint,
                    normalized_fingerprint=plan.normalized_fingerprint,
                    mapping_fingerprint=plan.mapping_fingerprint,
                    execution_plan_fingerprint=plan.fingerprint,
                    sealed_fingerprint=run.sealed_fingerprint,
                    policy_fingerprint=canonical_sha256_value(policy.canonical_json()),
                    inserted=plan.planned_inserts,
                    updated=plan.planned_updates,
                    skipped=plan.planned_skips,
                    quarantined=plan.planned_quarantine,
                    loaded_records=len({s.record_id for s in plan.steps} - rejected),
                    rejected_records=len(rejected),
                    generated_at=started_at,
                    warnings=("LOAD_QUARANTINED",) if plan.planned_quarantine else (),
                )
                if ledger is not None:
                    await ledger.commit(connection, receipt, plan)
                await permissions(
                    connection,
                    catalog,
                    policy.preflight,
                    mapping,
                    session_mode="writer",
                )
                # Новый reader проверяет и schema metadata вне table locks.
                await _catalog(
                    connection,
                    target,
                    names,
                    schemas,
                    version,
                    mapping.database_fingerprint,
                )
                check_deadline()
                commit_started = True
                await connection.commit()
                committed = True
    except asyncio.CancelledError:
        cancelled = True
    except TimeoutError:
        code = "PROCESSING_TIMEOUT"
    except StructuraGuardError as error:
        code = error.error_code
    except (SQLAlchemyError, PostgresError, OSError) as error:
        code = sql_error(error)
    except (ValueError, TypeError, RuntimeError, KeyError, AssertionError):
        code = "LOAD_DATABASE_FAILED"
    finally:
        cleanup_ok, cleanup_cancelled = await _cleanup(
            connection, engine, driver, target.limits.cleanup_seconds
        )
        cancelled = cancelled or cleanup_cancelled
    if committed:
        if not cleanup_ok:
            warnings += ("LOAD_POST_COMMIT_CLEANUP_FAILED",)
        if cancelled:
            warnings += ("LOAD_CANCELLED_AFTER_COMMIT",)
        return Outcome(StagingRunStatus.COMMITTED, plan, None, False, warnings, receipt)
    if commit_started or not cleanup_ok:
        return Outcome(
            StagingRunStatus.UNKNOWN, None, "LOAD_OUTCOME_UNKNOWN", cancelled
        )
    return Outcome(
        StagingRunStatus.CANCELLED if cancelled else StagingRunStatus.ROLLED_BACK,
        None,
        code or "LOAD_DATABASE_FAILED",
        cancelled,
    )
