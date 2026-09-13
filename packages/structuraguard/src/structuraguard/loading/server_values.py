"""Проверка явных разрешений server expressions без исполнения SQL из metadata."""

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.database import CatalogColumnRef, DatabaseCatalog
from structuraguard.contracts.loading import ServerValuePermission
from structuraguard.domain.constraint_semantics import unique_keys

from .projection import failure


def allowed_server_values(
    catalog: DatabaseCatalog, permissions: tuple[ServerValuePermission, ...]
) -> frozenset[CatalogColumnRef]:
    tables = {t.table_id: t for s in catalog.schemas for t in s.tables}
    result: set[CatalogColumnRef] = set()
    for permission in permissions:
        table = tables.get(permission.column.table_id)
        column = (
            None
            if table is None
            else next(
                (
                    c
                    for c in table.columns
                    if c.column_id == permission.column.column_id
                ),
                None,
            )
        )
        if (
            column is None
            or column.inspection is None
            or permission.evaluation_allowed is not True
            or canonical_sha256_value(column.inspection.canonical_json())
            != permission.metadata_fingerprint
        ):
            raise failure("LOAD_SERVER_VALUE_PERMISSION_INVALID")
        assert table is not None
        keys = {
            cid for k in unique_keys(table, catalog.dialect) for cid in k.column_ids
        }
        keys.update(cid for fk in table.foreign_keys for cid in fk.column_ids)
        if (
            column.column_id in keys
            or column.inspection.identity is not None
            or column.inspection.generation_storage == "virtual"
        ):
            raise failure("LOAD_SERVER_KEY_UNSUPPORTED")
        if column.inspection.default is None and not column.generated:
            raise failure("LOAD_SERVER_VALUE_PERMISSION_INVALID")
        result.add(permission.column)
    return frozenset(result)
