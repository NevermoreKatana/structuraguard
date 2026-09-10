"""Фиксированные параметризованные metadata queries; входной SQL не принимается.

Текст обрезается на сервере до sentinel длины до передачи клиенту. Sentinel
отклоняется reader: неполные metadata никогда не попадают в snapshot.
"""

STATE = """
SELECT current_setting('transaction_read_only') AS read_only,
       current_setting('transaction_isolation') AS isolation,
       current_setting('statement_timeout') AS statement_timeout,
       current_setting('server_version_num')::integer AS version
LIMIT :row_limit
"""

SCHEMA = """
SELECT n.oid::bigint AS oid, n.nspname AS name,
       pg_catalog.has_schema_privilege(n.oid, 'USAGE') AS usable
FROM pg_catalog.pg_namespace AS n WHERE n.nspname = :schema
LIMIT :row_limit
"""

SCHEMA_COMMENT = """
SELECT left(pg_catalog.obj_description(:oid, 'pg_namespace'), :text_limit) AS comment
LIMIT :row_limit
"""

RELATION = """
SELECT c.oid::bigint AS oid, c.relname AS name, c.relkind::text AS kind,
       left(pg_catalog.obj_description(c.oid, 'pg_class'), :text_limit) AS comment
FROM pg_catalog.pg_class AS c
WHERE c.relnamespace = :schema_oid AND c.relname = :table
LIMIT :row_limit
"""

COLUMNS = """
SELECT a.attnum::integer AS position, a.attname AS name,
       a.atttypid::bigint AS type_oid, a.atttypmod AS type_modifier,
       a.attnotnull AS not_null, a.attidentity::text AS identity,
       a.attgenerated::text AS generated,
       (a.attcollation = 0 OR a.attcollation = 'pg_catalog."default"'::pg_catalog.regcollation) AS default_collation,
       left(pg_catalog.format_type(a.atttypid, a.atttypmod), :text_limit) AS native_type,
       left(pg_catalog.pg_get_expr(d.adbin, d.adrelid), :text_limit) AS expression,
       left(pg_catalog.col_description(a.attrelid, a.attnum), :text_limit) AS comment
FROM pg_catalog.pg_attribute AS a
LEFT JOIN pg_catalog.pg_attrdef AS d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = :oid AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum LIMIT :row_limit
"""

TYPE = """
SELECT t.typtype::text AS kind, t.typcategory::text AS category,
       n.nspname AS schema, t.typbasetype::bigint AS base_oid,
       t.typelem::bigint AS element_oid, t.typtypmod AS modifier,
       t.typnotnull AS not_null,
       left(pg_catalog.format_type(t.oid, :modifier), :text_limit) AS native_type,
       left(pg_catalog.format_type(t.typbasetype, t.typtypmod), :text_limit) AS base_native,
       left(t.typdefault, :text_limit) AS default
FROM pg_catalog.pg_type AS t
JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
WHERE t.oid = :oid AND n.nspname = ANY(:schemas) LIMIT :row_limit
"""

ENUM = """
SELECT e.enumlabel AS label FROM pg_catalog.pg_enum AS e
WHERE e.enumtypid = :oid ORDER BY e.enumsortorder LIMIT :row_limit
"""

DOMAIN_CHECKS = """
SELECT c.conname AS name, c.convalidated AS validated,
       left(pg_catalog.pg_get_expr(c.conbin, 0), :text_limit) AS expression,
       left(pg_catalog.obj_description(c.oid, 'pg_constraint'), :text_limit) AS comment
FROM pg_catalog.pg_constraint AS c WHERE c.contypid = :oid AND c.contype = 'c'
ORDER BY c.conname LIMIT :row_limit
"""

CONSTRAINTS = """
SELECT c.conname AS name, c.contype::text AS kind,
       c.conkey::integer[] AS columns, c.confkey::integer[] AS foreign_columns,
       n.nspname AS foreign_schema, r.relname AS foreign_table,
       c.confupdtype::text AS on_update, c.confdeltype::text AS on_delete,
       c.confmatchtype::text AS match, c.condeferrable AS deferrable,
       c.condeferred AS initially_deferred, c.convalidated AS validated,
       c.connoinherit AS no_inherit, c.confdelsetcols::integer[] AS delete_columns,
       true AS enforced, false AS period,
       left(pg_catalog.pg_get_expr(c.conbin, c.conrelid), :text_limit) AS expression,
       left(pg_catalog.obj_description(c.oid, 'pg_constraint'), :text_limit) AS comment
FROM pg_catalog.pg_constraint AS c
LEFT JOIN pg_catalog.pg_class AS r ON r.oid = c.confrelid
LEFT JOIN pg_catalog.pg_namespace AS n ON n.oid = r.relnamespace
WHERE c.conrelid = :oid ORDER BY c.conname LIMIT :row_limit
"""

CONSTRAINTS_18 = CONSTRAINTS.replace(
    "true AS enforced, false AS period",
    "c.conenforced AS enforced, c.conperiod AS period",
)

INDEXES = """
SELECT i.indexrelid::bigint AS oid, c.relname AS name,
       i.indisunique AS unique, i.indisprimary AS primary,
       i.indisvalid AND i.indisready AS valid,
       i.indnullsnotdistinct AS nulls_not_distinct, i.indnkeyatts AS key_count,
       am.amname AS method,
       EXISTS(SELECT 1 FROM pg_catalog.pg_constraint AS con
              WHERE con.conindid = i.indexrelid AND con.contype = 'u') AS constraint,
       left(pg_catalog.pg_get_expr(i.indpred, i.indrelid), :text_limit) AS predicate,
       left(pg_catalog.obj_description(c.oid, 'pg_class'), :text_limit) AS comment
FROM pg_catalog.pg_index AS i
JOIN pg_catalog.pg_class AS c ON c.oid = i.indexrelid
JOIN pg_catalog.pg_am AS am ON am.oid = c.relam
WHERE i.indrelid = :oid ORDER BY c.relname LIMIT :row_limit
"""

INDEX_KEYS = """
SELECT k.position, i.indkey[k.position]::integer AS column_position,
       left(pg_catalog.pg_get_indexdef(i.indexrelid, k.position + 1, false), :text_limit) AS expression,
       (i.indoption[k.position] & 1) = 1 AS descending,
       (i.indoption[k.position] & 2) = 2 AS nulls_first,
       CASE WHEN coll.oid IS NULL THEN NULL
            ELSE pg_catalog.quote_ident(cn.nspname) || '.' || pg_catalog.quote_ident(coll.collname) END AS collation,
       CASE WHEN op.oid IS NULL THEN NULL
            ELSE pg_catalog.quote_ident(onsp.nspname) || '.' || pg_catalog.quote_ident(op.opcname) END AS operator_class
FROM pg_catalog.pg_index AS i
CROSS JOIN LATERAL pg_catalog.generate_series(0, i.indnatts - 1) AS k(position)
LEFT JOIN pg_catalog.pg_collation AS coll ON coll.oid = i.indcollation[k.position]
LEFT JOIN pg_catalog.pg_namespace AS cn ON cn.oid = coll.collnamespace
LEFT JOIN pg_catalog.pg_opclass AS op ON op.oid = i.indclass[k.position]
LEFT JOIN pg_catalog.pg_namespace AS onsp ON onsp.oid = op.opcnamespace
WHERE i.indexrelid = :oid ORDER BY k.position LIMIT :row_limit
"""
