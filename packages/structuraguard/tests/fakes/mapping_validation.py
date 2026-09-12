"""Связанные M7/M8 snapshots для независимой проверки MappingPlan."""

from collections.abc import AsyncIterator
from decimal import Decimal

from structuraguard.contracts.common import (
    IntegerScalar,
    LoadOperation,
    NormalizedScalar,
)
from structuraguard.contracts.database import (
    CatalogColumnRef,
    DatabaseCatalog,
    TableCatalog,
)
from structuraguard.contracts.mapping import FieldMapping, MappingPlan
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
)
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.profiling import NormalizedDataProfiler
from tests.fakes.mapping import catalog, column, scope_for, table
from tests.fakes.profiling import PRODUCER, normalized_stream


async def case(
    *, amount: NormalizedScalar | None = None, semantic_type: str = "integer"
) -> tuple[
    MappingPlan,
    NormalizedDatasetManifest,
    DatabaseCatalog,
    MappingValidationPolicy,
    NormalizedDataProfile,
]:
    """Создать простой insert с подтверждённым source PK."""
    batches = [
        b
        async for b in normalized_stream(
            [
                {
                    "id": IntegerScalar(value=1),
                    "amount": amount if amount is not None else IntegerScalar(value=12),
                }
            ],
            semantic_type=semantic_type,
        )
    ]
    manifest = batches[-1].manifest
    assert manifest is not None

    async def stream() -> AsyncIterator[NormalizedBatch]:
        for batch in batches:
            yield batch

    profile = await NormalizedDataProfiler().profile(stream())
    pk = column("id", "integer").model_copy(
        update={"nullable": False, "primary_key": True}
    )
    amount_column = column("amount", "integer", position=1)
    db = catalog(
        table("orders", column("id", "integer"), amount_column).model_copy(
            update={"primary_key": ("id",), "columns": (pk, amount_column)}
        )
    )
    policy = MappingValidationPolicy(
        policy_id="test",
        scope=scope_for(db),
        allow_schemas=("public",),
        allow_tables=(("public", "orders"),),
        source_identity_allow=(
            CatalogColumnRef(table_id="public.orders", column_id="id"),
        ),
    )
    plan = MappingPlan(
        plan_id="test",
        revision=1,
        source_fingerprint=manifest.source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        parse_plan_fingerprint=manifest.parse_plan_fingerprint,
        normalized_fingerprint=manifest.normalized_fingerprint,
        database_fingerprint=db.database_fingerprint,
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
        producer=PRODUCER,
        operation=LoadOperation.INSERT_ONLY,
        confidence=Decimal("1"),
        mappings=tuple(
            FieldMapping(
                source=f.ref,
                target=CatalogColumnRef(
                    table_id="public.orders",
                    column_id=f.field_name,
                ),
            )
            for f in manifest.semantic_schema
        ),
    )
    return plan, manifest, db, policy, profile


def replan(plan: MappingPlan, **updates: object) -> MappingPlan:
    """Пересчитать hash только при намеренном изменении плана."""
    data = plan.model_dump(mode="python")
    data.update(updates)
    data.pop("fingerprint")
    return MappingPlan.model_validate(data)


def bind_catalog(
    plan: MappingPlan,
    db: DatabaseCatalog,
    table: TableCatalog,
    names: tuple[str, str] = ("id", "amount"),
) -> tuple[MappingPlan, MappingValidationPolicy]:
    """Привязать два source fields к exact IDs реально отражённой таблицы."""
    columns = {c.name: c for c in table.columns}
    mappings = tuple(
        m.model_copy(
            update={
                "target": CatalogColumnRef(
                    table_id=table.table_id,
                    column_id=columns[name].column_id,
                )
            }
        )
        for m, name in zip(plan.mappings, names, strict=True)
    )
    policy = MappingValidationPolicy(
        policy_id="integration",
        scope=scope_for(db),
        allow_schemas=tuple(s.name for s in db.schemas),
        allow_tables=tuple(
            (t.schema_name, t.name) for s in db.schemas for t in s.tables
        ),
        source_identity_allow=tuple(m.target for m in mappings),
    )
    return replan(
        plan,
        mappings=mappings,
        database_fingerprint=db.database_fingerprint,
        target_id=db.target_id,
        target_policy_fingerprint=db.target_policy_fingerprint,
    ), policy
