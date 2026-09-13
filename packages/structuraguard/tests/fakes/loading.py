"""Связанные normalized snapshots для dry-run без внешнего ValidationReport."""

from collections.abc import Mapping, Sequence
from datetime import timedelta

from structuraguard.contracts.common import LoadOperation, NormalizedScalar
from structuraguard.contracts.constraint_validation import ConstraintReadPolicy
from structuraguard.contracts.database import (
    CatalogColumnRef,
    DatabaseCatalog,
    StagingContext,
)
from structuraguard.contracts.loading import DryRunPolicy, DryRunRequest, LoadRequest
from structuraguard.contracts.mapping import FieldMapping
from structuraguard.contracts.mapping_rules import MappingRelation
from structuraguard.contracts.mapping_validation import MappingValidationPolicy
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.staging import (
    StagingArtifactKind,
    StagingArtifactReference,
    StagingRunSpec,
)
from structuraguard.ports.stores import RunStagingStore
from tests.fakes.mapping import scope_for
from tests.fakes.mapping_validation import case, replan
from tests.fakes.profiling import normalized_stream
from tests.fakes.staging import StagingClock


async def dry_run_case(
    catalog: DatabaseCatalog,
    rows: Sequence[Mapping[str, NormalizedScalar]],
    *,
    table_name: str,
    operation: LoadOperation = LoadOperation.INSERT_ONLY,
    targets: Mapping[str, tuple[str, str]] | None = None,
    error_policy: str = "atomic",
    batch_size: int = 100,
    semantic_type: str = "integer",
    supplied_batches: tuple[NormalizedBatch, ...] | None = None,
) -> tuple[DryRunRequest, DryRunPolicy]:
    batches = (
        supplied_batches
        if supplied_batches is not None
        else tuple(
            [
                b
                async for b in normalized_stream(
                    rows, semantic_type=semantic_type, batch_size=batch_size
                )
            ]
        )
    )
    manifest = batches[-1].manifest
    assert manifest is not None
    tables = {t.name: t for s in catalog.schemas for t in s.tables}
    template, _, _, _, _ = await case()
    mappings: list[FieldMapping] = []
    for field in manifest.semantic_schema:
        target_name, name = (targets or {}).get(
            f"{field.entity_type}.{field.field_name}",
            (targets or {}).get(field.field_name, (table_name, field.field_name)),
        )
        target = tables[target_name]
        cid = next(c.column_id for c in target.columns if c.name == name)
        mappings.append(
            FieldMapping(
                source=field.ref,
                target=CatalogColumnRef(table_id=target.table_id, column_id=cid),
            )
        )
    by_target = {m.target: m.source for m in mappings}
    selected = {m.target.table_id for m in mappings}
    relations = []
    for table in tables.values():
        if table.table_id not in selected:
            continue
        for fk in table.foreign_keys:
            child_sources = tuple(
                by_target[CatalogColumnRef(table_id=table.table_id, column_id=cid)]
                for cid in fk.column_ids
            )
            relations.append(
                MappingRelation(
                    foreign_key_id=fk.foreign_key_id,
                    child_table_id=table.table_id,
                    parent_table_id=fk.referenced_table_id,
                    child_column_ids=fk.column_ids,
                    parent_column_ids=fk.referenced_column_ids,
                    child_sources=child_sources,
                    strategy="source_values",
                )
            )
    refs = tuple(
        CatalogColumnRef(table_id=t.table_id, column_id=c.column_id)
        for t in tables.values()
        for c in t.columns
    )
    policy = DryRunPolicy.model_validate(
        {
            "writer_principal": "dry_writer",
            "mapping_policy": MappingValidationPolicy(
                policy_id="dry_test",
                scope=scope_for(catalog),
                allow_schemas=tuple(s.name for s in catalog.schemas),
                allow_tables=tuple((t.schema_name, t.name) for t in tables.values()),
                source_identity_allow=refs,
                lookup_allow=refs,
            ),
            "read_policy": ConstraintReadPolicy(allow_columns=refs),
            "error_policy": error_policy,
        }
    )
    plan = replan(
        template,
        schema_version="1.1.0",
        source_fingerprint=manifest.source.source_fingerprint,
        extraction_fingerprint=manifest.extraction_fingerprint,
        parse_plan_fingerprint=manifest.parse_plan_fingerprint,
        normalized_fingerprint=manifest.normalized_fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        operation=operation,
        mappings=tuple(mappings),
        relations=tuple(relations),
    )
    return DryRunRequest(batches=batches, mapping=plan), policy


async def sealed_input(
    snapshot: DryRunRequest,
    store: RunStagingStore,
    clock: StagingClock,
    *,
    run_id: str = "load-1",
) -> LoadRequest:
    """Stage/seal выполняются явно в fixture, до вызова loader."""
    manifest = snapshot.batches[-1].manifest
    assert manifest is not None
    mapping = snapshot.mapping
    context = StagingContext(
        run_id=run_id,
        staging_id="load-staging",
        target_id=mapping.target_id,
        database_fingerprint=mapping.database_fingerprint,
        target_policy_fingerprint=mapping.target_policy_fingerprint,
        normalized_fingerprint=manifest.normalized_fingerprint,
        expires_at=clock.now + timedelta(hours=1),
        max_records=1000,
    )
    spec = StagingRunSpec(
        context=context,
        source_fingerprint=mapping.source_fingerprint,
        extraction_fingerprint=mapping.extraction_fingerprint,
        parse_plan_fingerprint=mapping.parse_plan_fingerprint,
        mapping_plan_fingerprint=mapping.fingerprint,
        batch_count=len(snapshot.batches),
        record_count=manifest.record_count,
        references=tuple(
            StagingArtifactReference(
                kind=kind,
                artifact_id=f"artifact-{kind}",
                fingerprint=manifest.normalized_fingerprint
                if kind == "normalized"
                else mapping.fingerprint
                if kind == "mapping"
                else mapping.source_fingerprint,
                retained_until=clock.now + timedelta(days=90),
            )
            for kind in StagingArtifactKind
        ),
    )
    await store.begin(spec)
    for batch in snapshot.batches:
        await store.stage(batch, context)
    run = await store.get_run(context)
    run = await store.seal(context, expected_revision=run.revision)
    return LoadRequest(
        snapshot=snapshot, staging_context=context, staging_revision=run.revision
    )
