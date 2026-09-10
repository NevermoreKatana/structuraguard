"""Проверка замкнутого metadata snapshot без инфраструктурных зависимостей."""

from structuraguard.contracts.database import DatabaseCatalog, DatabaseMetadataSnapshot
from structuraguard.exceptions import DatabaseInspectionError

type CatalogInput = DatabaseMetadataSnapshot | DatabaseCatalog


def validated_metadata(catalog: CatalogInput) -> DatabaseMetadataSnapshot:
    if isinstance(catalog, DatabaseMetadataSnapshot):
        return DatabaseMetadataSnapshot.model_validate(catalog.model_dump())
    if (
        catalog.schema_version != "1.1.0"
        or catalog.fingerprint_version != "catalog-v1"
        or catalog.comments_supported is None
    ):
        raise DatabaseInspectionError(
            error_code="DATABASE_METADATA_UNSUPPORTED",
            message="Для анализа требуются полные metadata версии catalog-v1.",
        )
    return DatabaseMetadataSnapshot.model_validate(
        {
            "dialect": catalog.dialect,
            "target_id": catalog.target_id,
            "target_policy_fingerprint": catalog.target_policy_fingerprint,
            "schemas": tuple(schema.model_dump() for schema in catalog.schemas),
            "comments_supported": catalog.comments_supported,
        }
    )
