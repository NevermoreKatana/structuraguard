"""Grants и непредставимая catalog-v1 семантика проверяются отдельно от hash."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from structuraguard.contracts.common import LoadOperation
from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.loading import DryRunPolicy
from structuraguard.contracts.mapping import MappingPlan
from structuraguard.loading.projection import failure

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncConnection


async def principal(
    connection: AsyncConnection,
    writer_principal: str,
    *,
    session_mode: Literal["inspection", "writer"] = "inspection",
) -> None:
    from sqlalchemy import text

    row = (
        await connection.execute(
            text("""
        SELECT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolcreaterole
            AND NOT r.rolcreatedb AND NOT r.rolbypassrls
            AND ((:session_mode = 'inspection' AND r.rolname <> current_user)
                OR (:session_mode = 'writer' AND r.rolname = current_user))
            AND current_user = session_user
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend owned
                WHERE owned.deptype='o'
                    AND owned.dbid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
                    AND owned.refclassid='pg_catalog.pg_authid'::regclass
                    AND pg_catalog.pg_has_role(r.oid, owned.refobjid, 'MEMBER'))
            AND NOT pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE')
            AND pg_catalog.has_database_privilege(r.oid, current_database(), 'CONNECT')
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n
                WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                AND (pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE')
                    OR pg_catalog.pg_has_role(r.oid, n.nspowner, 'MEMBER')))
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
                    AND pg_catalog.pg_has_role(r.oid, c.relowner, 'MEMBER'))
            AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_roles privileged
                WHERE (privileged.rolname IN ('pg_read_server_files', 'pg_write_server_files', 'pg_execute_server_program', 'pg_signal_backend', 'pg_checkpoint', 'pg_maintain')
                    OR privileged.rolsuper OR privileged.rolcreaterole
                    OR privileged.rolcreatedb OR privileged.rolbypassrls)
                AND pg_catalog.pg_has_role(r.oid, privileged.oid, 'MEMBER')) AS allowed
        FROM pg_catalog.pg_roles r WHERE r.rolname=:writer
    """),
            {"writer": writer_principal, "session_mode": session_mode},
        )
    ).one_or_none()
    if row is None or row[0] is not True:
        raise failure("DRY_RUN_WRITER_NOT_ALLOWED")


async def permissions(
    connection: AsyncConnection,
    catalog: DatabaseCatalog,
    policy: DryRunPolicy,
    mapping: MappingPlan,
    *,
    session_mode: Literal["inspection", "writer"] = "inspection",
) -> None:
    from sqlalchemy import text

    await principal(connection, policy.writer_principal, session_mode=session_mode)
    tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
    selected = {m.target.table_id for m in mapping.mappings}
    read_ids = {ref.table_id for ref in policy.read_policy.allow_columns}
    if not selected | read_ids <= tables.keys():
        raise failure("TARGET_NOT_ALLOWED")
    for tid in sorted(selected | read_ids):
        table = tables[tid]
        row = (
            await connection.execute(
                text("""
            SELECT c.oid,
                c.relkind::text='r' AND NOT c.relrowsecurity AND NOT c.relforcerowsecurity
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal)
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_rewrite r WHERE r.ev_class=c.oid)
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid)
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_index i
                    JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid
                    JOIN pg_catalog.pg_am am ON am.oid=ix.relam
                    WHERE i.indrelid=c.oid AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL
                        OR NOT i.indisvalid OR NOT i.indisready OR am.amname <> 'btree'
                        OR EXISTS (SELECT 1 FROM pg_catalog.unnest(i.indclass) AS key(opclass)
                            JOIN pg_catalog.pg_opclass op ON op.oid=key.opclass
                            JOIN pg_catalog.pg_namespace ns ON ns.oid=op.opcnamespace
                            WHERE ns.nspname <> 'pg_catalog'))) AS safe,
                pg_catalog.has_schema_privilege(CAST(:writer AS name),n.oid,'USAGE'),
                pg_catalog.has_schema_privilege(current_user,n.oid,'USAGE'),
                pg_catalog.has_table_privilege(CAST(:writer AS name),c.oid,'DELETE,TRUNCATE,TRIGGER')
            FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=:schema AND c.relname=:table
        """),
                {
                    "writer": policy.writer_principal,
                    "schema": table.schema_name,
                    "table": table.name,
                },
            )
        ).one_or_none()
        if row is None or row[1] is not True:
            raise failure("DRY_RUN_DATABASE_SEMANTICS_UNVERIFIED")
        if row[2] is not True or row[3] is not True or row[4] is not False:
            raise failure("DRY_RUN_PERMISSION_DENIED")
        oid = row[0]
        writes = {
            m.target.column_id for m in mapping.mappings if m.target.table_id == tid
        }
        reads = {
            r.column_id for r in policy.read_policy.allow_columns if r.table_id == tid
        }
        by_id = {c.column_id: c for c in table.columns}
        if not writes | reads <= by_id.keys():
            raise failure("TARGET_NOT_ALLOWED")
        for cid in sorted(writes | reads):
            grants = (
                await connection.execute(
                    text("""
                SELECT pg_catalog.has_column_privilege(CAST(:writer AS name),CAST(:oid AS oid),CAST(:column AS text),'INSERT'),
                    pg_catalog.has_column_privilege(CAST(:writer AS name),CAST(:oid AS oid),CAST(:column AS text),'UPDATE'),
                    pg_catalog.has_column_privilege(CAST(:writer AS name),CAST(:oid AS oid),CAST(:column AS text),'SELECT'),
                    pg_catalog.has_column_privilege(current_user,CAST(:oid AS oid),CAST(:column AS text),'SELECT')
            """),
                    {
                        "writer": policy.writer_principal,
                        "oid": oid,
                        "column": by_id[cid].name,
                    },
                )
            ).one()
            if (
                cid in writes
                and (
                    grants[0] is not True
                    or (
                        mapping.operation is LoadOperation.UPSERT
                        and grants[1] is not True
                    )
                )
            ) or (cid in reads and (grants[2] is not True or grants[3] is not True)):
                raise failure("DRY_RUN_PERMISSION_DENIED")
