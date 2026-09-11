"""Малые настоящие M7/M8 snapshots для проверки mapper без I/O."""

from collections.abc import Mapping
from decimal import Decimal

from structuraguard.contracts.common import NormalizedScalar, StringScalar
from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    ColumnInspectionMetadata,
    DatabaseCatalog,
    DatabaseMetadataSnapshot,
    DatabaseType,
    ForeignKeyCatalog,
    SchemaCatalog,
    TableCatalog,
    TableInspectionMetadata,
)
from structuraguard.contracts.deterministic_mapping import MappingScope, MappingWeights
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.domain.database_fingerprint import database_fingerprint
from structuraguard.domain.database_graph import build_dependency_graph
from structuraguard.profiling import NormalizedDataProfiler
from tests.fakes.profiling import PRODUCER, normalized_stream

POLICY = "sha256:" + "a" * 64


def column(name: str, kind: str = "text", *, position: int = 0) -> ColumnCatalog:
    """Построить колонку с проверенным canonical type."""
    data_type = DatabaseType.model_validate(
        {"native_type": kind, "canonical_type": kind}
    )
    return ColumnCatalog(
        column_id=name,
        name=name,
        type_name=kind,
        nullable=True,
        inspection=ColumnInspectionMetadata(
            data_type=data_type, ordinal_position=position
        ),
    )


def table(
    name: str,
    *columns: ColumnCatalog,
    schema: str = "public",
    foreign_keys: tuple[ForeignKeyCatalog, ...] = (),
) -> TableCatalog:
    """Qualified имена позволяют проверять одинаковые column names."""
    return TableCatalog(
        table_id=f"{schema}.{name}",
        schema_name=schema,
        name=name,
        columns=columns,
        foreign_keys=foreign_keys,
        inspection=TableInspectionMetadata(),
    )


def catalog(*tables: TableCatalog) -> DatabaseCatalog:
    """Собрать полноценный catalog-v1, включая derived graph."""
    schemas = tuple(
        SchemaCatalog(
            schema_id=name,
            name=name,
            tables=tuple(t for t in tables if t.schema_name == name),
        )
        for name in sorted({t.schema_name for t in tables})
    )
    snapshot = DatabaseMetadataSnapshot(
        dialect="postgresql",
        target_id="test",
        target_policy_fingerprint=POLICY,
        schemas=schemas,
        comments_supported=True,
    )
    return DatabaseCatalog(
        schema_version="1.1.0",
        dialect=snapshot.dialect,
        target_id=snapshot.target_id,
        target_policy_fingerprint=POLICY,
        producer=PRODUCER,
        schemas=schemas,
        database_fingerprint=database_fingerprint(snapshot),
        fingerprint_version="catalog-v1",
        comments_supported=True,
        dependency_graph=build_dependency_graph(snapshot),
    )


def refs(value: DatabaseCatalog) -> tuple[CatalogColumnRef, ...]:
    """Список всех колонок, явно разрешаемый владельцем теста."""
    return tuple(
        CatalogColumnRef(table_id=t.table_id, column_id=c.column_id)
        for s in value.schemas
        for t in s.tables
        for c in t.columns
    )


async def profile(
    values: Mapping[str, NormalizedScalar] | None = None,
    *,
    entity: str = "row",
) -> NormalizedDataProfile:
    """Получить завершённый профиль вместо непроверенного model_construct."""
    row = (
        values
        if values is not None
        else {"email": StringScalar(value="person@example.org")}
    )
    return await NormalizedDataProfiler().profile(
        normalized_stream([row] * 25 if row else [], entity_type=entity)
    )


def scope_for(db: DatabaseCatalog) -> MappingScope:
    """Явно разрешить все колонки малого тестового catalog."""
    return MappingScope(
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        allow=refs(db),
    )


def only_weight(signal: str) -> MappingWeights:
    """Проверить hard blockers при максимальном весе одного сигнала."""
    return MappingWeights.model_validate(
        {name: Decimal(int(name == signal)) for name in MappingWeights.model_fields}
    )


def rehash_profile(
    data: NormalizedDataProfile, **updates: object
) -> NormalizedDataProfile:
    """Связать изменённый fixture с новым честно вычисленным profile hash."""
    return NormalizedDataProfile.model_validate(
        data.model_copy(
            update={**updates, "profile_fingerprint": "sha256:" + "0" * 64}
        ).model_dump(mode="python")
    )
