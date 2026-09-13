"""Фиксированная ledger schema: runtime проверяет layout, но никогда не делает DDL."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.loading.projection import failure

if TYPE_CHECKING:
    from sqlalchemy import MetaData, Table
    from sqlalchemy.ext.asyncio import AsyncConnection


def tables(schema: str) -> tuple[MetaData, dict[str, Table]]:
    from sqlalchemy import Column, Integer, MetaData, Table, Text

    metadata = MetaData(schema=schema)
    Table(
        "schema_info",
        metadata,
        Column("version", Integer, primary_key=True, autoincrement=False),
        Column("fingerprint", Text, nullable=False),
    )
    for name in ("execution_commits", "execution_audit", "execution_quarantine"):
        columns = [
            Column("scope_hash", Text, primary_key=True),
            Column("key_hash", Text, primary_key=True),
        ]
        if name == "execution_quarantine":
            columns.append(Column("unit_hash", Text, primary_key=True))
        columns.extend(
            (
                Column("binding", Text, nullable=False),
                Column("payload", Text, nullable=False),
                Column("fingerprint", Text, nullable=False),
            )
        )
        Table(name, metadata, *columns)
    return metadata, {t.name: t for t in metadata.tables.values()}


def layout_fingerprint(schema: str) -> str:
    metadata, _ = tables(schema)
    return canonical_sha256_value(
        (
            "loader-ledger-v1",
            schema,
            tuple(
                (
                    t.name,
                    tuple(
                        (c.name, str(c.type), c.nullable, c.primary_key)
                        for c in t.columns
                    ),
                )
                for t in sorted(metadata.tables.values(), key=lambda t: t.name)
            ),
        )
    )


async def check_schema(connection: AsyncConnection, schema: str) -> None:
    """Lock фиксированных таблиц, полная shape/PK/runtime проверка и version."""
    from sqlalchemy import Integer, func, select, text

    _, expected = tables(schema)
    quote = connection.dialect.identifier_preparer.quote_identifier
    for name in sorted(expected):
        await connection.exec_driver_sql(
            f"LOCK TABLE ONLY {quote(schema)}.{quote(name)} IN ACCESS SHARE MODE"
        )
    names = tuple(expected)
    rows = (
        (
            await connection.execute(
                text("""
        SELECT c.relname, c.relkind::text, c.relrowsecurity, c.relforcerowsecurity,
            c.relhasrules,
            EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS triggers,
            EXISTS(SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS inherits,
            EXISTS(SELECT 1 FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid
                JOIN pg_catalog.pg_am am ON am.oid=ix.relam
                WHERE i.indrelid=c.oid AND (NOT i.indisprimary OR NOT i.indisvalid OR NOT i.indisready
                    OR i.indexprs IS NOT NULL OR i.indpred IS NOT NULL OR am.amname <> 'btree'
                    OR EXISTS(SELECT 1 FROM pg_catalog.unnest(i.indclass) k(opclass)
                        JOIN pg_catalog.pg_opclass op ON op.oid=k.opclass
                        WHERE op.opcnamespace <> 'pg_catalog'::regnamespace))) AS unsafe_index,
            a.attname, pg_catalog.format_type(a.atttypid,a.atttypmod) AS type,
            a.attnotnull, a.atthasdef, a.attgenerated::text, a.attidentity::text,
            a.attcollation IN (0,'pg_catalog."default"'::regcollation) AS default_collation
        FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid
        WHERE n.nspname=:schema AND c.relname=ANY(CAST(:names AS text[]))
            AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum LIMIT 64
    """),
                {"schema": schema, "names": names},
            )
        )
        .mappings()
        .all()
    )
    wanted = {
        (
            t.name,
            c.name,
            "integer" if isinstance(c.type, Integer) else "text",
            not c.nullable,
        )
        for t in expected.values()
        for c in t.columns
    }
    if {
        (r["relname"], r["attname"], r["type"], r["attnotnull"]) for r in rows
    } != wanted or any(
        r["relkind"] != "r"
        or r["relrowsecurity"]
        or r["relforcerowsecurity"]
        or r["relhasrules"]
        or r["triggers"]
        or r["inherits"]
        or r["unsafe_index"]
        or r["atthasdef"]
        or r["attgenerated"]
        or r["attidentity"]
        or not r["default_collation"]
        for r in rows
    ):
        raise failure("LOAD_LEDGER_SCHEMA_INVALID")
    keys = (
        (
            await connection.execute(
                text("""
        SELECT c.relname, k.contype::text, k.condeferrable, k.convalidated,
            ARRAY(SELECT a.attname FROM pg_catalog.unnest(k.conkey) WITH ORDINALITY x(num,ord)
                JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=x.num ORDER BY x.ord) AS columns
        FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname=:schema AND c.relname=ANY(CAST(:names AS text[])) AND k.contype <> 'n' LIMIT 16
    """),
                {"schema": schema, "names": names},
            )
        )
        .mappings()
        .all()
    )
    if len(keys) != len(expected) or any(
        k["contype"] != "p"
        or k["condeferrable"]
        or not k["convalidated"]
        or tuple(k["columns"])
        != tuple(c.name for c in expected[k["relname"]].primary_key.columns)
        for k in keys
    ):
        raise failure("LOAD_LEDGER_SCHEMA_INVALID")
    info = expected["schema_info"]
    version = (
        await connection.execute(
            select(info.c.version, func.left(info.c.fingerprint, 72)).limit(2)
        )
    ).all()
    if len(version) != 1 or tuple(version[0]) != (1, layout_fingerprint(schema)):
        raise failure("LOAD_LEDGER_SCHEMA_INVALID")


async def grants(connection: AsyncConnection, schema: str) -> None:
    from sqlalchemy import text

    quote = connection.dialect.identifier_preparer.quote_identifier
    for name in tables(schema)[1]:
        row = (
            await connection.execute(
                text("""
            SELECT pg_catalog.has_schema_privilege(current_user, :schema, 'USAGE'),
                pg_catalog.has_table_privilege(current_user, :table, 'SELECT'),
                pg_catalog.has_table_privilege(current_user, :table, 'INSERT'),
                pg_catalog.has_table_privilege(current_user, :table, 'UPDATE,DELETE,TRUNCATE,TRIGGER')
                    OR pg_catalog.has_any_column_privilege(current_user, :table, 'UPDATE'),
                pg_catalog.has_any_column_privilege(current_user, :table, 'INSERT')
        """),
                {"schema": schema, "table": f"{quote(schema)}.{quote(name)}"},
            )
        ).one()
        if (
            row[0] is not True
            or row[1] is not True
            or row[3] is not False
            or (name != "schema_info" and row[2] is not True)
            or (name == "schema_info" and (row[2] is not False or row[4] is not False))
        ):
            raise failure("LOAD_LEDGER_PERMISSION_DENIED")
