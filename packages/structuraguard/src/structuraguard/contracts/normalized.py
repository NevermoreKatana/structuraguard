"""Семантическая модель, создаваемая только после применения ParsePlan."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Annotated, Self

from pydantic import Field, StrictBool, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import (
    BatchFingerprint,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    NormalizedScalar,
    PhysicalSourceRef,
    ProducerMetadata,
    RawScalar,
    SchemaVersionStr,
    SourceArtifactRef,
)
from structuraguard.contracts.execution import PhysicalValueOrigin, SelectionTrace


def _reject_duplicate_refs(refs: tuple[PhysicalSourceRef, ...], *, field: str) -> None:
    if len(refs) != len(set(refs)):
        raise ValueError(f"{field} содержит повторяющиеся physical references")


class NormalizedValue(FrozenContract):
    """Типизированное значение с обязательной physical provenance."""

    value_id: IdentifierStr
    field_name: IdentifierStr
    raw_value: RawScalar
    normalized_value: NormalizedScalar
    semantic_type: IdentifierStr
    source_refs: tuple[PhysicalSourceRef, ...]
    transformations: tuple[IdentifierStr, ...] = ()
    issue_codes: tuple[IdentifierStr, ...] = ()
    origins: Annotated[tuple[PhysicalValueOrigin, ...], Field(max_length=64)] = Field(
        default=(), exclude_if=lambda value: not value
    )
    selection: SelectionTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_value(self) -> Self:
        if bool(self.origins) != (self.selection is not None):
            raise ValueError("origins и selection указываются вместе")
        if any(origin.source_ref not in self.source_refs for origin in self.origins):
            raise ValueError("origins выходят за physical provenance")
        if len({origin.source_ref for origin in self.origins}) != len(self.origins):
            raise ValueError("origins должны быть уникальны")
        if self.selection is not None:
            operation = self.selection.operation.value
            if self.transformations != (() if operation == "copy" else (operation,)):
                raise ValueError("transformations не совпадают с selection")
            if operation == "copy" and (
                len(self.origins) != 1 or self.raw_value != self.origins[0].raw_value
            ):
                raise ValueError("copy не соответствует physical raw value")
        if not self.source_refs:
            raise ValueError("normalized value требует physical provenance")
        _reject_duplicate_refs(self.source_refs, field="source_refs")
        if len({ref.extraction_id for ref in self.source_refs}) > 1:
            raise ValueError("normalized value смешивает extraction runs")
        if len(self.transformations) != len(set(self.transformations)):
            raise ValueError("transformations должны быть уникальны")
        if len(self.issue_codes) != len(set(self.issue_codes)):
            raise ValueError("issue_codes должны быть уникальны")
        if not self.transformations and self.raw_value != self.normalized_value:
            raise ValueError("Изменение raw value требует transformation evidence")
        if self.semantic_type == "money" and self.normalized_value.kind != "decimal":
            raise ValueError("money должен храниться как Decimal")
        return self


class SemanticField(FrozenContract):
    """Поле семантической схемы без сведений о целевой БД."""

    entity_type: IdentifierStr
    field_name: IdentifierStr
    semantic_type: IdentifierStr

    @property
    def ref(self) -> SemanticFieldRef:
        """Вернуть точную ссылку на поле сущности этой схемы."""

        return SemanticFieldRef(
            entity_type=self.entity_type,
            field_name=self.field_name,
        )


class SemanticFieldRef(FrozenContract):
    """Проверяемая ссылка на поле нормализованной сущности."""

    entity_type: IdentifierStr
    field_name: IdentifierStr


class SemanticEntity(FrozenContract):
    """Семантическая сущность с физически проверяемым происхождением."""

    entity_id: IdentifierStr
    entity_type: IdentifierStr
    values: tuple[NormalizedValue, ...]
    source_refs: tuple[PhysicalSourceRef, ...]
    parent_entity_id: IdentifierStr | None = None

    @model_validator(mode="after")
    def _validate_entity(self) -> Self:
        if not self.values:
            raise ValueError("semantic entity требует хотя бы одно значение")
        if not self.source_refs:
            raise ValueError("semantic entity требует physical provenance")
        _reject_duplicate_refs(self.source_refs, field="source_refs")
        provenance = (
            *self.source_refs,
            *(ref for value in self.values for ref in value.source_refs),
        )
        if len({ref.extraction_id for ref in provenance}) > 1:
            raise ValueError("semantic entity смешивает extraction runs")
        if len({value.value_id for value in self.values}) != len(self.values):
            raise ValueError("value_id должны быть уникальны внутри entity")
        if len({value.field_name for value in self.values}) != len(self.values):
            raise ValueError("field_name должны быть уникальны внутри entity")
        if self.parent_entity_id == self.entity_id:
            raise ValueError("entity не может быть собственным parent")
        available_refs = set(self.source_refs)
        if any(
            ref not in available_refs
            for value in self.values
            for ref in value.source_refs
        ):
            raise ValueError("entity provenance не покрывает provenance значений")
        return self


class NormalizedRecord(FrozenContract):
    """Группа семантических сущностей одной логической записи."""

    record_id: IdentifierStr
    entities: tuple[SemanticEntity, ...]
    source_refs: tuple[PhysicalSourceRef, ...]
    parent_record_id: IdentifierStr | None = None
    related_record_ids: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        if not self.entities:
            raise ValueError("normalized record требует хотя бы одну entity")
        if not self.source_refs:
            raise ValueError("normalized record требует physical provenance")
        _reject_duplicate_refs(self.source_refs, field="source_refs")
        provenance = (
            *self.source_refs,
            *(ref for entity in self.entities for ref in entity.source_refs),
        )
        if len({ref.extraction_id for ref in provenance}) > 1:
            raise ValueError("normalized record смешивает extraction runs")
        if len({entity.entity_id for entity in self.entities}) != len(self.entities):
            raise ValueError("entity_id должны быть уникальны внутри record")
        if len(self.related_record_ids) != len(set(self.related_record_ids)):
            raise ValueError("related_record_ids должны быть уникальны")
        if self.record_id == self.parent_record_id:
            raise ValueError("record не может быть собственным parent")
        if self.record_id in self.related_record_ids:
            raise ValueError("record не может ссылаться на себя")
        available_refs = set(self.source_refs)
        if any(
            ref not in available_refs
            for entity in self.entities
            for ref in entity.source_refs
        ):
            raise ValueError("record provenance не покрывает provenance сущностей")
        parents = {
            entity.entity_id: entity.parent_entity_id for entity in self.entities
        }
        if any(
            parent is not None and parent not in parents for parent in parents.values()
        ):
            raise ValueError("entity ссылается на отсутствующий parent")
        self._reject_entity_cycles(parents)
        return self

    @staticmethod
    def _reject_entity_cycles(parents: dict[str, str | None]) -> None:
        resolved: set[str] = set()
        for entity_id in parents:
            visited: set[str] = set()
            current: str | None = entity_id
            while current is not None and current not in resolved:
                if current in visited:
                    raise ValueError("semantic entities не могут содержать cycle")
                visited.add(current)
                current = parents[current]
            resolved.update(visited)


class SemanticSourceIndex(FrozenContract):
    """Компактный индекс допустимых semantic field references."""

    fields: tuple[SemanticFieldRef, ...] = ()

    @model_validator(mode="after")
    def _validate_fields(self) -> Self:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("semantic field references должны быть уникальны")
        return self


class NormalizedBatchSummary(BatchFingerprint):
    """Сериализуемая lineage и cardinality одного normalized batch."""

    source: SourceArtifactRef
    extraction_id: IdentifierStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    producer: ProducerMetadata
    record_count: NonNegativeInt
    entity_count: NonNegativeInt
    value_count: NonNegativeInt

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.entity_count < self.record_count:
            raise ValueError("Каждая normalized record требует entity")
        if self.value_count < self.entity_count:
            raise ValueError("Каждая semantic entity требует value")
        return self


class NormalizedDatasetManifest(FrozenContract):
    """Aggregate fingerprint и индекс завершённого normalized stream."""

    schema_version: SchemaVersionStr = "1.0.0"
    source: SourceArtifactRef
    extraction_id: IdentifierStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    producer: ProducerMetadata
    batches: tuple[NormalizedBatchSummary, ...]
    normalized_fingerprint: FingerprintStr
    record_count: NonNegativeInt = 0
    entity_count: NonNegativeInt = 0
    value_count: NonNegativeInt = 0
    semantic_fields: tuple[IdentifierStr, ...] = ()
    semantic_index: SemanticSourceIndex = SemanticSourceIndex()
    semantic_schema: tuple[SemanticField, ...] = ()

    @model_validator(mode="after")
    def _validate_manifest(self) -> Self:
        if self.schema_version not in {"1.0.0", "1.1.0", "1.2.0"}:
            raise ValueError("неподдерживаемая версия NormalizedDatasetManifest")
        if self.schema_version in {"1.1.0", "1.2.0"}:
            expected = canonical_sha256_value(
                self, exclude_top_level=frozenset({"normalized_fingerprint"})
            )
            if self.normalized_fingerprint == "sha256:" + "0" * 64:
                object.__setattr__(self, "normalized_fingerprint", expected)
            elif self.normalized_fingerprint != expected:
                raise ValueError(
                    "normalized manifest fingerprint не совпадает с payload"
                )
        indices = tuple(batch.batch_index for batch in self.batches)
        if not indices or indices != tuple(range(len(indices))):
            raise ValueError("normalized batches должны образовывать диапазон от нуля")
        fingerprints = tuple(batch.batch_fingerprint for batch in self.batches)
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("normalized batch fingerprints должны быть уникальны")
        for batch in self.batches:
            if batch.source != self.source:
                raise ValueError("normalized batch summary относится к другому source")
            if batch.extraction_id != self.extraction_id:
                raise ValueError(
                    "normalized batch summary относится к другому extraction"
                )
            if batch.extraction_fingerprint != self.extraction_fingerprint:
                raise ValueError(
                    "normalized batch summary имеет другой extraction fingerprint"
                )
            if batch.parse_plan_fingerprint != self.parse_plan_fingerprint:
                raise ValueError(
                    "normalized batch summary имеет другой parse plan fingerprint"
                )
            if batch.producer != self.producer:
                raise ValueError("normalized batch summary создан другим producer")
        expected_counts = (
            sum(batch.record_count for batch in self.batches),
            sum(batch.entity_count for batch in self.batches),
            sum(batch.value_count for batch in self.batches),
        )
        if expected_counts != (
            self.record_count,
            self.entity_count,
            self.value_count,
        ):
            raise ValueError(
                "record_count/entity_count/value_count не совпадают с batch summaries"
            )
        if len(self.semantic_fields) != len(set(self.semantic_fields)):
            raise ValueError("semantic_fields должны быть уникальны")
        schema_refs = tuple(field.ref for field in self.semantic_schema)
        if len(schema_refs) != len(set(schema_refs)):
            raise ValueError("semantic schema содержит повторяющиеся fields")
        if set(schema_refs) != set(self.semantic_index.fields):
            raise ValueError("semantic schema и semantic index должны совпадать")
        schema_names = {field.field_name for field in self.semantic_schema}
        if schema_names != set(self.semantic_fields):
            raise ValueError("semantic_fields и semantic schema должны совпадать")
        return self

    def semantic_type_for(self, reference: SemanticFieldRef) -> str | None:
        """Вернуть semantic type точного entity/field reference."""

        return next(
            (
                field.semantic_type
                for field in self.semantic_schema
                if field.ref == reference
            ),
            None,
        )

    def validate_batch(self, batch: NormalizedBatch) -> None:
        """Сверить один normalized batch с lineage, schema и hashes manifest.

        Args:
            batch: Batch, который должен принадлежать этому manifest.

        Returns:
            None при согласованном batch; полный stream этим не подтверждается.

        Raises:
            ValueError: Не совпали version, fingerprint, summary, terminal marker
                либо semantic schema.

        Для schema 1.1.0 перепроверяются payload hashes manifest и batch, в том
        числе после model_copy. Legacy 1.0.0 не обещает проверяемые hashes.
        Объекты не меняются, I/O нет; hash не удостоверяет подлинность source.
        """

        self._verify_fingerprint()
        self._validate_bound_batch(batch)

    def _verify_fingerprint(self) -> None:
        if (
            self.schema_version in {"1.1.0", "1.2.0"}
            and canonical_sha256_value(
                self, exclude_top_level=frozenset({"normalized_fingerprint"})
            )
            != self.normalized_fingerprint
        ):
            raise ValueError("normalized manifest fingerprint не совпадает с payload")

    def _validate_bound_batch(self, batch: NormalizedBatch) -> None:
        if batch.schema_version != self.schema_version:
            raise ValueError("normalized schema versions не совпадают")
        if (
            self.schema_version in {"1.1.0", "1.2.0"}
            and canonical_sha256_value(
                batch, exclude_top_level=frozenset({"batch_fingerprint", "manifest"})
            )
            != batch.batch_fingerprint
        ):
            raise ValueError("normalized batch payload изменён")

        if batch.batch_index >= len(self.batches):
            raise ValueError("Normalized batch отсутствует в manifest sequence")
        expected = self.batches[batch.batch_index]
        if batch.to_summary() != expected:
            raise ValueError("Normalized batch summary не совпадает с manifest")
        expected_terminal = batch.batch_index == len(self.batches) - 1
        if batch.is_last != expected_terminal:
            raise ValueError("Terminal marker не совпадает с normalized manifest")
        if expected_terminal and batch.manifest != self:
            raise ValueError("Terminal normalized batch содержит другой manifest")
        actual_schema = {
            SemanticField(
                entity_type=entity.entity_type,
                field_name=value.field_name,
                semantic_type=value.semantic_type,
            )
            for record in batch.records
            for entity in record.entities
            for value in entity.values
        }
        if not actual_schema <= set(self.semantic_schema):
            raise ValueError("Normalized batch не согласован с semantic schema")

    def validate_batches(self, batches: Iterable[NormalizedBatch]) -> None:
        """Проверить весь normalized stream и глобальную уникальность semantic IDs.

        Args:
            batches: Полная sync последовательность от batch_index=0 до terminal.

        Returns:
            None после успешной проверки всего stream.

        Raises:
            ValueError: Неверны manifest/batch hashes schema 1.1.0, порядок,
                counts, lineage/schema либо уникальность record/entity/value IDs.

        Iterator потребляется без удержания raw batches. Множества IDs растут
        с числом records/entities/values; для большого output нужен бюджет caller.
        Hash manifest считается один раз. Метод не пишет данные, не закрывает
        iterator caller и не заменяет downstream staging/rollback.
        """

        # Один hash manifest на проход; повторять его для каждого batch — O(B²).
        self._verify_fingerprint()
        record_ids: set[str] = set()
        entity_ids: set[str] = set()
        value_ids: set[str] = set()
        batch_count = 0
        record_count = 0
        entity_count = 0
        value_count = 0
        for expected_index, batch in enumerate(batches):
            if expected_index >= len(self.batches):
                raise ValueError(
                    "Количество normalized batches не совпадает с manifest"
                )
            if batch.batch_index != expected_index:
                raise ValueError("Порядок normalized batches не совпадает с manifest")
            self._validate_bound_batch(batch)
            batch_count += 1
            for record in batch.records:
                if record.record_id in record_ids:
                    raise ValueError("record_id должны быть глобально уникальны")
                record_ids.add(record.record_id)
                record_count += 1
                for entity in record.entities:
                    if entity.entity_id in entity_ids:
                        raise ValueError("entity_id должны быть глобально уникальны")
                    entity_ids.add(entity.entity_id)
                    entity_count += 1
                    for value in entity.values:
                        if value.value_id in value_ids:
                            raise ValueError("value_id должны быть глобально уникальны")
                        value_ids.add(value.value_id)
                        value_count += 1
        if batch_count != len(self.batches):
            raise ValueError("Количество normalized batches не совпадает с manifest")
        if (record_count, entity_count, value_count) != (
            self.record_count,
            self.entity_count,
            self.value_count,
        ):
            raise ValueError("Фактические normalized counts не совпадают с manifest")


class NormalizedBatch(FrozenContract):
    """Batch семантических records с проверяемой цепочкой provenance."""

    schema_version: SchemaVersionStr = "1.0.0"
    source: SourceArtifactRef
    extraction_id: IdentifierStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    producer: ProducerMetadata
    batch_index: NonNegativeInt
    batch_fingerprint: FingerprintStr
    records: tuple[NormalizedRecord, ...] = ()
    is_last: StrictBool = False
    manifest: NormalizedDatasetManifest | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> Self:
        if self.schema_version not in {"1.0.0", "1.1.0", "1.2.0"}:
            raise ValueError("неподдерживаемая версия NormalizedBatch")
        if self.schema_version in {"1.1.0", "1.2.0"}:
            expected = canonical_sha256_value(
                self, exclude_top_level=frozenset({"batch_fingerprint", "manifest"})
            )
            if self.batch_fingerprint == "sha256:" + "0" * 64:
                object.__setattr__(self, "batch_fingerprint", expected)
            elif self.batch_fingerprint != expected:
                raise ValueError("normalized batch fingerprint не совпадает с payload")
            if any(
                origin.location.source != self.source
                for record in self.records
                for entity in record.entities
                for value in entity.values
                for origin in value.origins
            ):
                raise ValueError("origin location относится к другому source")
        elif any(
            value.origins
            for record in self.records
            for entity in record.entities
            for value in entity.values
        ):
            raise ValueError("точные origins требуют normalized schema 1.1.0")
        if self.schema_version != "1.2.0" and any(
            origin.source_spans
            for record in self.records
            for entity in record.entities
            for value in entity.values
            for origin in value.origins
        ):
            raise ValueError("Source spans требуют normalized schema 1.2.0")
        if self.is_last != (self.manifest is not None):
            raise ValueError("только terminal batch должен содержать manifest")
        if len({record.record_id for record in self.records}) != len(self.records):
            raise ValueError("record_id должны быть уникальны внутри batch")
        self._validate_record_links()
        entities = tuple(
            entity for record in self.records for entity in record.entities
        )
        if len({entity.entity_id for entity in entities}) != len(entities):
            raise ValueError("entity_id должны быть уникальны внутри batch")
        values = tuple(value for entity in entities for value in entity.values)
        if len({value.value_id for value in values}) != len(values):
            raise ValueError("value_id должны быть уникальны внутри batch")

        refs = tuple(
            ref
            for record in self.records
            for ref in (
                *record.source_refs,
                *(item for entity in record.entities for item in entity.source_refs),
                *(
                    item
                    for entity in record.entities
                    for value in entity.values
                    for item in value.source_refs
                ),
            )
        )
        if any(ref.extraction_id != self.extraction_id for ref in refs):
            raise ValueError("normalized batch содержит reference другого extraction")

        if self.manifest is None:
            return self
        if self.source != self.manifest.source:
            raise ValueError("normalized manifest относится к другому source")
        if self.extraction_id != self.manifest.extraction_id:
            raise ValueError("normalized manifest относится к другому extraction")
        if self.extraction_fingerprint != self.manifest.extraction_fingerprint:
            raise ValueError("normalized manifest имеет другой extraction fingerprint")
        if self.parse_plan_fingerprint != self.manifest.parse_plan_fingerprint:
            raise ValueError("normalized manifest имеет другой parse plan fingerprint")
        if self.producer != self.manifest.producer:
            raise ValueError("normalized manifest создан другим producer")
        if self.manifest.batches[-1].batch_index != self.batch_index:
            raise ValueError("terminal batch должен завершать manifest sequence")
        batch_fingerprints = {
            batch.batch_index: batch.batch_fingerprint
            for batch in self.manifest.batches
        }
        if batch_fingerprints.get(self.batch_index) != self.batch_fingerprint:
            raise ValueError("terminal batch fingerprint не совпадает с manifest")
        current_fields = {
            value.field_name
            for record in self.records
            for entity in record.entities
            for value in entity.values
        }
        if not current_fields <= set(self.manifest.semantic_fields):
            raise ValueError("normalized manifest не содержит semantic fields batch")
        current_refs = {
            SemanticFieldRef(
                entity_type=entity.entity_type,
                field_name=value.field_name,
            )
            for record in self.records
            for entity in record.entities
            for value in entity.values
        }
        if not current_refs <= set(self.manifest.semantic_index.fields):
            raise ValueError("normalized manifest не содержит semantic refs batch")
        current_schema = {
            SemanticField(
                entity_type=entity.entity_type,
                field_name=value.field_name,
                semantic_type=value.semantic_type,
            )
            for record in self.records
            for entity in record.entities
            for value in entity.values
        }
        if not current_schema <= set(self.manifest.semantic_schema):
            raise ValueError("normalized manifest не содержит semantic schema batch")
        self.manifest.validate_batch(self)
        return self

    def to_summary(self) -> NormalizedBatchSummary:
        """Вернуть serializable lineage/count summary без normalized values."""

        entities = tuple(
            entity for record in self.records for entity in record.entities
        )
        return NormalizedBatchSummary(
            batch_index=self.batch_index,
            batch_fingerprint=self.batch_fingerprint,
            source=self.source,
            extraction_id=self.extraction_id,
            extraction_fingerprint=self.extraction_fingerprint,
            parse_plan_fingerprint=self.parse_plan_fingerprint,
            producer=self.producer,
            record_count=len(self.records),
            entity_count=len(entities),
            value_count=sum(len(entity.values) for entity in entities),
        )

    def _validate_record_links(self) -> None:
        records = {record.record_id: record for record in self.records}
        for record in self.records:
            references = (
                *((record.parent_record_id,) if record.parent_record_id else ()),
                *record.related_record_ids,
            )
            if any(reference not in records for reference in references):
                raise ValueError("record ссылается на отсутствующую запись")

        resolved: set[str] = set()
        for record_id in records:
            visited: set[str] = set()
            current: str | None = record_id
            while current is not None and current not in resolved:
                if current in visited:
                    raise ValueError("normalized records не могут содержать cycle")
                visited.add(current)
                current = records[current].parent_record_id
            resolved.update(visited)
