"""Фиксированная схема staging и точечная runtime-проверка без reflection-all."""

from __future__ import annotations

from typing import TYPE_CHECKING

from structuraguard.contracts._base import canonical_sha256_value

from ._core import Access, failure

if TYPE_CHECKING:
    from sqlalchemy import MetaData, Table
    from sqlalchemy.ext.asyncio import AsyncConnection
    from sqlalchemy.schema import SchemaItem


def tables(schema: str) -> tuple[MetaData, dict[str, Table]]:
    from sqlalchemy import (
        Column,
        ForeignKeyConstraint,
        Integer,
        MetaData,
        Table,
        Text,
        UniqueConstraint,
    )

    metadata = MetaData(schema=schema)
    Table(
        "schema_info",
        metadata,
        Column("version", Integer, primary_key=True, autoincrement=False),
        Column("fingerprint", Text, nullable=False),
    )
    for name, ordinal in (
        ("runs", None),
        ("batches", "batch_index"),
        ("records", "record_index"),
    ):
        cols: list[SchemaItem] = [
            Column("namespace", Text, primary_key=True),
            Column("target_id", Text, primary_key=True),
            Column("run_id", Text, primary_key=True),
        ]
        if ordinal is not None:
            cols.append(Column(ordinal, Integer, primary_key=True))
        if name == "records":
            cols.append(Column("record_id", Text, nullable=False))
        cols.extend(
            (
                Column("payload", Text, nullable=False),
                Column("fingerprint", Text, nullable=False),
            )
        )
        table = Table(name, metadata, *cols)
        if name != "runs":
            table.append_constraint(
                ForeignKeyConstraint(
                    ["namespace", "target_id", "run_id"],
                    [
                        f"{schema}.runs.{c}"
                        for c in ("namespace", "target_id", "run_id")
                    ],
                )
            )
        if name == "records":
            table.append_constraint(
                UniqueConstraint("namespace", "target_id", "run_id", "record_id")
            )
    return metadata, {t.name: t for t in metadata.tables.values()}


def layout_fingerprint(schema: str) -> str:
    metadata, _ = tables(schema)
    # SQL compiler обходит constraint sets без гарантированного порядка;
    # version binding задаётся канонической structural projection.
    return canonical_sha256_value(
        (
            "staging-v1",
            schema,
            tuple(
                (
                    t.name,
                    tuple(
                        (c.name, str(c.type), c.nullable, c.primary_key)
                        for c in t.columns
                    ),
                )
                for t in sorted(metadata.tables.values(), key=lambda table: table.name)
            ),
        )
    )


async def check_schema(
    connection: AsyncConnection, schema: str, access: Access = "read"
) -> None:
    """Проверить ровно четыре таблицы, columns/constraints, RLS/triggers и version."""
    from sqlalchemy import Integer, func, select, text
    from sqlalchemy.schema import (
        ForeignKeyConstraint,
        PrimaryKeyConstraint,
        UniqueConstraint,
    )

    _, expected = tables(schema)
    quote = connection.dialect.identifier_preparer.quote_identifier
    # Имена фиксированы, schema прошла закрытую проверку target. LOCK сохраняет
    # relation binding до конца transaction; source identifiers сюда не попадают.
    for name in sorted(expected):
        mode = (
            "ROW EXCLUSIVE"
            if access != "read" and name != "schema_info"
            else "ACCESS SHARE"
        )
        await connection.exec_driver_sql(
            f"LOCK TABLE ONLY {quote(schema)}.{quote(name)} IN {mode} MODE"
        )
    actual = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname, c.relkind::text AS relkind, c.relrowsecurity, c.relforcerowsecurity, c.relhasrules, "
                    "EXISTS(SELECT 1 FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid AND NOT t.tgisinternal) AS triggers, "
                    "EXISTS(SELECT 1 FROM pg_catalog.pg_inherits i WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) AS inherits, "
                    "EXISTS(SELECT 1 FROM pg_catalog.pg_index i "
                    "JOIN pg_catalog.pg_class ix ON ix.oid=i.indexrelid "
                    "JOIN pg_catalog.pg_am am ON am.oid=ix.relam WHERE i.indrelid=c.oid "
                    "AND (i.indexprs IS NOT NULL OR i.indpred IS NOT NULL OR NOT i.indisvalid OR NOT i.indisready "
                    "OR am.amname <> 'btree' "
                    "OR NOT EXISTS(SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid=i.indexrelid AND k.contype IN ('p','u')) "
                    "OR EXISTS(SELECT 1 FROM pg_catalog.unnest(i.indclass) key(opclass) "
                    "JOIN pg_catalog.pg_opclass op ON op.oid=key.opclass WHERE op.opcnamespace <> 'pg_catalog'::regnamespace))) AS unsafe_index, "
                    "a.attname, pg_catalog.format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull, a.attgenerated::text AS generated, "
                    "a.attidentity::text AS identity, a.atthasdef, "
                    "a.attcollation IN (0,'pg_catalog.\"default\"'::regcollation) AS default_collation "
                    "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                    "WHERE n.nspname=:schema AND c.relname IN ('schema_info','runs','batches','records') "
                    "AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum LIMIT 64"
                ),
                {"schema": schema},
            )
        )
        .mappings()
        .all()
    )
    shape = {
        (row["relname"], row["attname"], row["type"], row["attnotnull"])
        for row in actual
    }
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
    if shape != wanted or any(
        r["relkind"] != "r"
        or r["relrowsecurity"]
        or r["relforcerowsecurity"]
        or r["relhasrules"]
        or r["generated"]
        or r["identity"]
        or r["atthasdef"]
        or r["unsafe_index"]
        or not r["default_collation"]
        or r["triggers"]
        or r["inherits"]
        for r in actual
    ):
        raise failure("STAGING_SCHEMA_UNAVAILABLE")
    constraints = (
        (
            await connection.execute(
                text(
                    "SELECT c.relname,k.contype::text AS contype,k.condeferrable,k.convalidated, "
                    "ARRAY(SELECT a.attname FROM unnest(k.conkey) WITH ORDINALITY AS x(num,ord) "
                    "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=x.num ORDER BY x.ord) AS columns, "
                    "ARRAY(SELECT a.attname FROM unnest(k.confkey) WITH ORDINALITY AS x(num,ord) "
                    "JOIN pg_catalog.pg_attribute a ON a.attrelid=k.confrelid AND a.attnum=x.num ORDER BY x.ord) AS parent_columns, "
                    "k.confupdtype::text AS update_action, k.confdeltype::text AS delete_action, k.confmatchtype::text AS match_type, "
                    "CASE WHEN k.contype='f' THEN k.confrelid = pg_catalog.to_regclass(:runs) ELSE true END AS parent_ok "
                    "FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=:schema AND c.relname IN ('schema_info','runs','batches','records') "
                    "AND k.contype <> 'n' LIMIT 32"
                ),
                {"schema": schema, "runs": f'{quote(schema)}."runs"'},
            )
        )
        .mappings()
        .all()
    )
    wanted_keys: set[tuple[str, str, tuple[str, ...]]] = set()
    for table in expected.values():
        for constraint in table.constraints:
            if not isinstance(
                constraint,
                (PrimaryKeyConstraint, UniqueConstraint, ForeignKeyConstraint),
            ):
                raise failure("STAGING_SCHEMA_UNAVAILABLE")
            kind = (
                "p"
                if isinstance(constraint, PrimaryKeyConstraint)
                else "u"
                if isinstance(constraint, UniqueConstraint)
                else "f"
                if isinstance(constraint, ForeignKeyConstraint)
                else "?"
            )
            wanted_keys.add(
                (table.name, kind, tuple(c.name for c in constraint.columns))
            )
    found_keys = {
        (r["relname"], r["contype"], tuple(r["columns"])) for r in constraints
    }
    if (
        found_keys != wanted_keys
        or len(constraints) != len(wanted_keys)
        or any(
            r["condeferrable"]
            or not r["convalidated"]
            or not r["parent_ok"]
            or (
                r["contype"] == "f"
                and (
                    tuple(r["parent_columns"]) != ("namespace", "target_id", "run_id")
                    or r["update_action"] != "a"
                    or r["delete_action"] != "a"
                    or r["match_type"] != "s"
                )
            )
            for r in constraints
        )
    ):
        raise failure("STAGING_SCHEMA_UNAVAILABLE")
    info = expected["schema_info"]
    version = (
        await connection.execute(
            select(info.c.version, func.left(info.c.fingerprint, 72)).limit(2)
        )
    ).all()
    if len(version) != 1 or tuple(version[0]) != (1, layout_fingerprint(schema)):
        raise failure("STAGING_SCHEMA_UNAVAILABLE")
