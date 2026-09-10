"""K7: version gate действует и на входах публичных domain consumers."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from structuraguard.contracts import (
    DatabaseCatalog,
    DatabaseMetadataSnapshot,
    ProducerMetadata,
)
from structuraguard.domain import (
    build_dependency_graph,
    canonical_database_catalog,
    database_fingerprint,
)
from structuraguard.exceptions import DatabaseInspectionError


def full_catalog(snapshot: DatabaseMetadataSnapshot) -> DatabaseCatalog:
    return DatabaseCatalog(
        schema_version="1.1.0",
        fingerprint_version="catalog-v1",
        dialect=snapshot.dialect,
        target_id=snapshot.target_id,
        target_policy_fingerprint=snapshot.target_policy_fingerprint,
        schemas=snapshot.schemas,
        comments_supported=snapshot.comments_supported,
        producer=ProducerMetadata(
            component_id="test", component_version="1.0.0", sdk_version="0.3.0"
        ),
        database_fingerprint=database_fingerprint(snapshot),
        dependency_graph=build_dependency_graph(snapshot),
    )


@pytest.mark.parametrize("version", ["1.0.0", "9.0.0"])
def test_extended_catalog_rejects_wrong_wire_version(
    catalog_snapshot: DatabaseMetadataSnapshot, version: str
) -> None:
    catalog = full_catalog(catalog_snapshot)
    with pytest.raises(ValidationError):
        DatabaseCatalog.model_validate(
            {**catalog.model_dump(), "schema_version": version}
        )


@pytest.mark.parametrize(
    "consumer",
    [canonical_database_catalog, database_fingerprint, build_dependency_graph],
)
def test_domain_consumer_rechecks_catalog_version(
    catalog_snapshot: DatabaseMetadataSnapshot,
    consumer: Callable[[DatabaseCatalog], object],
) -> None:
    catalog = full_catalog(catalog_snapshot).model_copy(
        update={"schema_version": "9.0.0"}
    )
    with pytest.raises(DatabaseInspectionError) as caught:
        consumer(catalog)
    assert caught.value.error_code == "DATABASE_METADATA_UNSUPPORTED"
