"""Сборка fingerprint-bound catalog после закрытия inspection connection."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Awaitable, Callable

from structuraguard.contracts.common import ProducerMetadata
from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseMetadataSnapshot,
    ForeignKeyCatalog,
    SchemaCatalog,
    TableCatalog,
)
from structuraguard.domain.database_fingerprint import database_fingerprint
from structuraguard.domain.database_graph import build_dependency_graph

from ._inspection import InspectionControl, failure, run_inspection
from .normalization import catalog_identifier
from .target import InspectionLimits


def _assemble(
    snapshot: DatabaseMetadataSnapshot, control: InspectionControl
) -> DatabaseCatalog:
    for schema in snapshot.schemas:
        control.account((schema.name,))
        for table in schema.tables:
            control.account((table.name,))
            assert table.inspection is not None
            for item in (
                *table.columns,
                *table.foreign_keys,
                *table.inspection.indexes,
                *table.inspection.constraints,
            ):
                control.account((item.model_dump_json(),))
    snapshot = _stable_foreign_keys(snapshot, control)
    fingerprint = database_fingerprint(snapshot)
    control.check()
    graph = build_dependency_graph(snapshot)
    control.check()
    result = DatabaseCatalog(
        schema_version="1.1.0",
        dialect=snapshot.dialect,
        target_id=snapshot.target_id,
        target_policy_fingerprint=snapshot.target_policy_fingerprint,
        database_fingerprint=fingerprint,
        fingerprint_version="catalog-v1",
        comments_supported=snapshot.comments_supported,
        dependency_graph=graph,
        producer=ProducerMetadata(
            component_id="database.inspector",
            component_version="1.0.0",
            sdk_version="0.3.0",
        ),
        schemas=snapshot.schemas,
    )
    if (
        len(result.model_dump_json().encode("utf-8"))
        > control.limits.max_metadata_bytes
    ):
        raise failure("SECURITY_LIMIT_EXCEEDED")
    control.check()
    return result


def _stable_foreign_keys(
    snapshot: DatabaseMetadataSnapshot, control: InspectionControl
) -> DatabaseMetadataSnapshot:
    schemas: list[SchemaCatalog] = []
    for schema in snapshot.schemas:
        tables: list[TableCatalog] = []
        for table in schema.tables:
            control.check()
            occurrences: Counter[str] = Counter()
            keys: list[ForeignKeyCatalog] = []
            for key in table.foreign_keys:
                identity = catalog_identifier(
                    "fk",
                    table.table_id,
                    key.referenced_table_id,
                    *key.column_ids,
                    *key.referenced_column_ids,
                    key.inspection.canonical_json() if key.inspection else "null",
                )
                occurrences[identity] += 1
                # Счётчик различает только полностью одинаковые constraints;
                # перестановка reflection не меняет множество полученных IDs.
                identity = catalog_identifier(
                    "fk", identity, str(occurrences[identity])
                )
                keys.append(key.model_copy(update={"foreign_key_id": identity}))
            tables.append(
                table.model_copy(
                    update={
                        "foreign_keys": tuple(
                            sorted(keys, key=lambda key: key.foreign_key_id)
                        )
                    }
                )
            )
        schemas.append(schema.model_copy(update={"tables": tuple(tables)}))
    return snapshot.model_copy(update={"schemas": tuple(schemas)})


async def inspect_catalog(
    inspect_metadata: Callable[[], Awaitable[DatabaseMetadataSnapshot]],
    limits: InspectionLimits,
) -> DatabaseCatalog:
    deadline = time.monotonic() + limits.timeout_seconds
    snapshot = await inspect_metadata()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise failure("PROCESSING_TIMEOUT")
    # CPU work не блокирует event loop; deadline включает reflection и cleanup.
    return await run_inspection(
        lambda control: _assemble(snapshot, control),
        limits.model_copy(update={"timeout_seconds": remaining}),
    )
