"""Ленивый normalized stream без хранения исходного dataset."""

from collections.abc import AsyncGenerator, Iterable, Mapping

from structuraguard.contracts.common import (
    NormalizedScalar,
    PhysicalObjectKind,
    PhysicalSourceRef,
    ProducerMetadata,
    SourceArtifactRef,
)
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedBatchSummary,
    NormalizedDatasetManifest,
    NormalizedRecord,
    NormalizedValue,
    SemanticEntity,
    SemanticField,
    SemanticSourceIndex,
)

AUTO = "sha256:" + "0" * 64
SOURCE = SourceArtifactRef(
    artifact_id="source", source_fingerprint="sha256:" + "1" * 64
)
PRODUCER = ProducerMetadata(
    component_id="test", component_version="1.0.0", sdk_version="0.3.0"
)
EXTRACTION = "extraction-00000000-0000-4000-8000-000000000001"
REF = PhysicalSourceRef(
    extraction_id=EXTRACTION,
    batch_index=0,
    kind=PhysicalObjectKind.LINE,
    local_id="line-00000000-0000-4000-8000-000000000001",
)


async def normalized_stream(
    rows: Iterable[Mapping[str, NormalizedScalar]],
    *,
    batch_size: int = 100,
    prefix: str = "a",
    entity_type: str = "row",
    semantic_type: str = "unresolved",
) -> AsyncGenerator[NormalizedBatch, None]:
    """Создать schema 1.1.0 с отдельным пустым terminal batch."""
    summaries: list[NormalizedBatchSummary] = []
    schema: dict[str, SemanticField] = {}
    pending: list[NormalizedRecord] = []
    for ordinal, row in enumerate(rows):
        values = tuple(
            NormalizedValue(
                value_id=f"{prefix}_v_{ordinal}_{i}",
                field_name=name,
                raw_value=value,
                normalized_value=value,
                semantic_type=semantic_type,
                source_refs=(REF,),
            )
            for i, (name, value) in enumerate(row.items())
        )
        for name in row:
            schema[name] = SemanticField(
                entity_type=entity_type, field_name=name, semantic_type=semantic_type
            )
        pending.append(
            NormalizedRecord(
                record_id=f"{prefix}_r_{ordinal}",
                source_refs=(REF,),
                entities=(
                    SemanticEntity(
                        entity_id=f"{prefix}_e_{ordinal}",
                        entity_type=entity_type,
                        values=values,
                        source_refs=(REF,),
                    ),
                ),
            )
        )
        if len(pending) >= batch_size:
            batch = make_batch(tuple(pending), len(summaries))
            summaries.append(batch.to_summary())
            yield batch
            pending.clear()
    if pending:
        batch = make_batch(tuple(pending), len(summaries))
        summaries.append(batch.to_summary())
        yield batch
    terminal = make_batch((), len(summaries), terminal=True)
    summaries.append(terminal.to_summary())
    fields = tuple(schema.values())
    manifest = NormalizedDatasetManifest(
        schema_version="1.1.0",
        source=SOURCE,
        extraction_id=EXTRACTION,
        extraction_fingerprint=AUTO,
        parse_plan_fingerprint=AUTO,
        producer=PRODUCER,
        batches=tuple(summaries),
        normalized_fingerprint=AUTO,
        record_count=sum(b.record_count for b in summaries),
        entity_count=sum(b.entity_count for b in summaries),
        value_count=sum(b.value_count for b in summaries),
        semantic_fields=tuple(schema),
        semantic_schema=fields,
        semantic_index=SemanticSourceIndex(fields=tuple(f.ref for f in fields)),
    )
    yield NormalizedBatch.model_validate(
        terminal.model_copy(update={"manifest": manifest}).model_dump(mode="python")
    )


def make_batch(
    records: tuple[NormalizedRecord, ...], index: int, *, terminal: bool = False
) -> NormalizedBatch:
    """Terminal заготовка нужна для вычисления summary до создания manifest."""
    if terminal:
        from structuraguard.contracts._base import canonical_sha256_value

        batch = NormalizedBatch.model_construct(
            schema_version="1.1.0",
            source=SOURCE,
            extraction_id=EXTRACTION,
            extraction_fingerprint=AUTO,
            parse_plan_fingerprint=AUTO,
            producer=PRODUCER,
            batch_index=index,
            batch_fingerprint=AUTO,
            records=records,
            is_last=True,
        )
        return batch.model_copy(
            update={
                "batch_fingerprint": canonical_sha256_value(
                    batch,
                    exclude_top_level=frozenset({"batch_fingerprint", "manifest"}),
                )
            }
        )
    return NormalizedBatch(
        schema_version="1.1.0",
        source=SOURCE,
        extraction_id=EXTRACTION,
        extraction_fingerprint=AUTO,
        parse_plan_fingerprint=AUTO,
        producer=PRODUCER,
        batch_index=index,
        batch_fingerprint=AUTO,
        records=records,
    )


def make_record(
    entities: Iterable[tuple[str, Mapping[str, NormalizedScalar]]],
    ordinal: int = 0,
    *,
    prefix: str = "audit",
) -> NormalizedRecord:
    """Record с несколькими, в том числе повторными, semantic entities."""
    return NormalizedRecord(
        record_id=f"{prefix}_r_{ordinal}",
        source_refs=(REF,),
        entities=tuple(
            SemanticEntity(
                entity_id=f"{prefix}_e_{ordinal}_{i}",
                entity_type=kind,
                source_refs=(REF,),
                values=tuple(
                    NormalizedValue(
                        value_id=f"{prefix}_v_{ordinal}_{i}_{j}",
                        field_name=name,
                        semantic_type="unresolved",
                        raw_value=value,
                        normalized_value=value,
                        source_refs=(REF,),
                    )
                    for j, (name, value) in enumerate(row.items())
                ),
            )
            for i, (kind, row) in enumerate(entities)
        ),
    )


async def record_stream(
    groups: Iterable[tuple[NormalizedRecord, ...]],
    *,
    schema_only: tuple[SemanticField, ...] = (),
) -> AsyncGenerator[NormalizedBatch, None]:
    """Завершить поток целых record link groups валидным manifest."""
    summaries: list[NormalizedBatchSummary] = []
    schema = {field.ref: field for field in schema_only}
    for records in groups:
        batch = make_batch(records, len(summaries))
        summaries.append(batch.to_summary())
        for record in records:
            for entity in record.entities:
                for value in entity.values:
                    field = SemanticField(
                        entity_type=entity.entity_type,
                        field_name=value.field_name,
                        semantic_type=value.semantic_type,
                    )
                    schema[field.ref] = field
        yield batch
    terminal = make_batch((), len(summaries), terminal=True)
    summaries.append(terminal.to_summary())
    manifest = NormalizedDatasetManifest(
        schema_version="1.1.0",
        source=SOURCE,
        extraction_id=EXTRACTION,
        extraction_fingerprint=AUTO,
        parse_plan_fingerprint=AUTO,
        producer=PRODUCER,
        batches=tuple(summaries),
        normalized_fingerprint=AUTO,
        record_count=sum(b.record_count for b in summaries),
        entity_count=sum(b.entity_count for b in summaries),
        value_count=sum(b.value_count for b in summaries),
        semantic_fields=tuple(sorted({f.field_name for f in schema.values()})),
        semantic_schema=tuple(schema.values()),
        semantic_index=SemanticSourceIndex(fields=tuple(schema)),
    )
    yield NormalizedBatch.model_validate(
        terminal.model_copy(update={"manifest": manifest}).model_dump(mode="python")
    )
