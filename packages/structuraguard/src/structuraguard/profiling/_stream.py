"""Preflight и точная bounded проверка normalized stream независимо от samples."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedBatchSummary,
    NormalizedDatasetManifest,
    SemanticFieldRef,
)
from structuraguard.contracts.profiling import NormalizedProfilingOptions
from structuraguard.exceptions import NormalizedProfilingError


def failure(reason: str, *, code: str = "INVALID_STREAM") -> NormalizedProfilingError:
    return NormalizedProfilingError(
        error_code="NORMALIZED_PROFILE_" + code,
        message="Профилирование нормализованных данных не завершено",
        details={"reason": reason},
    )


def limit(resource: str) -> NormalizedProfilingError:
    return failure(resource, code="LIMIT_EXCEEDED")


class Ledger:
    """Консервативный retained budget; не process RSS quota."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.current = 0
        self.peak = 0

    def add(self, size: int) -> None:
        if self.current + size > self.maximum:
            raise limit("retained_state_bytes")
        self.current += size
        self.peak = max(self.peak, self.current)


def _preflight_size(
    value: object,
    options: NormalizedProfilingOptions,
    *,
    maximum: int | None = None,
    skip_manifest: bool = False,
) -> int:
    """Bound вложений и allocation до model_dump; rejects forged oversized DTO."""
    cap = options.max_batch_bytes if maximum is None else maximum
    stack = [(value, 0)]
    items = size = 0
    while stack:
        item, depth = stack.pop()
        items += 1
        size += 64
        if items > options.max_batch_items and maximum is None:
            raise limit("batch_items")
        if depth > options.max_depth:
            raise limit("depth")
        if isinstance(item, str | bytes):
            if len(item) > options.max_scalar_bytes:
                raise limit("scalar_bytes")
            length = len(item.encode("utf-8")) if isinstance(item, str) else len(item)
            if length > options.max_scalar_bytes:
                raise limit("scalar_bytes")
            size += length * 4
        elif type(item) is int:
            if item.bit_length() > options.max_numeric_digits * 3.322 + 1:
                raise limit("numeric_digits")
            if len(str(abs(item))) > options.max_numeric_digits:
                raise limit("numeric_digits")
            size += item.bit_length() // 8
        elif isinstance(item, Decimal):
            if (
                not item.is_finite()
                or item.__sizeof__() > options.max_numeric_digits * 4 + 256
            ):
                raise limit("numeric_digits")
            parts = item.as_tuple()
            if len(parts.digits) > options.max_numeric_digits:
                raise limit("numeric_digits")
            if (
                not isinstance(parts.exponent, int)
                or abs(parts.exponent) > options.max_decimal_exponent
            ):
                raise limit("decimal_exponent")
            size += len(parts.digits) * 4
        elif isinstance(item, BaseModel):
            names = type(item).model_fields
            if size + (len(stack) + len(names)) * 64 > cap:
                raise limit("input_bytes")
            stack.extend(
                (getattr(item, name), depth + 1)
                for name in names
                if not (skip_manifest and depth == 0 and name == "manifest")
            )
        elif isinstance(item, tuple | list):
            if size + (len(stack) + len(item)) * 64 > cap:
                raise limit("input_bytes")
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, bool | float | date | datetime):
            raise failure("unsupported_input_type")
        if size > cap:
            raise limit("input_bytes")
    return size


def preflight(
    value: object,
    options: NormalizedProfilingOptions,
    *,
    maximum: int | None = None,
    skip_manifest: bool = False,
) -> int:
    """Одинаковая safe boundary для batch, manifest, context и classifier DTO."""
    try:
        return _preflight_size(
            value, options, maximum=maximum, skip_manifest=skip_manifest
        )
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise failure("input_contract") from None


class StreamCheck:
    """Точные ID sets/summaries имеют независимые и общие конечные бюджеты."""

    def __init__(self, options: NormalizedProfilingOptions, ledger: Ledger) -> None:
        self.options = options
        self.ledger = ledger
        self.summaries: list[NormalizedBatchSummary] = []
        self.ids: set[tuple[str, str]] = set()
        self.id_bytes = 0
        self.schema: dict[SemanticFieldRef, str] = {}
        self.entity_types: set[str] = set()
        self.manifest: NormalizedDatasetManifest | None = None
        self.version: str | None = None

    def accept(self, batch: NormalizedBatch) -> NormalizedBatch:
        if type(batch) is not NormalizedBatch:
            raise failure("batch_type")
        if self.manifest is not None:
            raise failure("after_terminal")
        if type(batch.schema_version) is not str:
            raise failure("batch_contract")
        if batch.schema_version not in ("1.1.0", "1.2.0"):
            raise failure("legacy_schema", code="UNSUPPORTED_SCHEMA")
        if len(self.summaries) >= self.options.max_batches:
            raise limit("batches")
        try:
            preflight(batch, self.options, skip_manifest=True)
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise failure("batch_preflight") from None
        try:
            if batch.manifest is not None:
                preflight(
                    batch.manifest,
                    self.options,
                    maximum=self.options.max_manifest_bytes,
                )
                if len(batch.manifest.batches) > self.options.max_batches:
                    raise limit("batches")
            checked = NormalizedBatch.model_validate(
                batch.model_dump(mode="python", warnings="error")
            )
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise failure("batch_contract") from None
        if checked.batch_index != len(self.summaries):
            raise failure("sequence")
        if self.version is not None and checked.schema_version != self.version:
            raise failure("schema_changed")
        self.version = checked.schema_version
        summary = checked.to_summary()
        if self.summaries:
            first = self.summaries[0]
            if (
                summary.source,
                summary.extraction_id,
                summary.extraction_fingerprint,
                summary.parse_plan_fingerprint,
                summary.producer,
            ) != (
                first.source,
                first.extraction_id,
                first.extraction_fingerprint,
                first.parse_plan_fingerprint,
                first.producer,
            ):
                raise failure("lineage")
        self.ledger.add(2048)
        self.summaries.append(summary)
        for record in checked.records:
            self._id("record", record.record_id)
            for entity in record.entities:
                self._id("entity", entity.entity_id)
                if entity.entity_type not in self.entity_types:
                    if len(self.entity_types) >= self.options.max_entity_types:
                        raise limit("entity_types")
                    self.entity_types.add(entity.entity_type)
                    self.ledger.add(256 + len(entity.entity_type) * 4)
                for value in entity.values:
                    self._id("value", value.value_id)
                    self._field(
                        SemanticFieldRef(
                            entity_type=entity.entity_type, field_name=value.field_name
                        ),
                        value.semantic_type,
                    )
        if checked.manifest is not None:
            manifest = checked.manifest
            if manifest.batches != tuple(self.summaries):
                raise failure("manifest_summaries")
            for field in manifest.semantic_schema:
                self._field(field.ref, field.semantic_type)
            if set(self.schema) != set(manifest.semantic_index.fields):
                raise failure("manifest_schema")
            self.manifest = manifest
        return checked

    def _field(self, field: SemanticFieldRef, declared: str) -> None:
        if field.entity_type not in self.entity_types:
            if len(self.entity_types) >= self.options.max_entity_types:
                raise limit("entity_types")
            self.ledger.add(256 + len(field.entity_type) * 4)
            self.entity_types.add(field.entity_type)
        if field not in self.schema:
            if len(self.schema) >= self.options.max_fields:
                raise limit("fields")
            self.ledger.add(
                512
                + (len(field.entity_type) + len(field.field_name) + len(declared)) * 4
            )
            self.schema[field] = declared
        elif self.schema[field] != declared:
            raise failure("semantic_schema_conflict")

    def _id(self, kind: str, value: str) -> None:
        key = (kind, value)
        if key in self.ids:
            raise failure("duplicate_ids")
        length = len(value.encode("utf-8"))
        if (
            len(self.ids) >= self.options.max_ids
            or self.id_bytes + length > self.options.max_id_bytes
        ):
            raise limit("global_ids")
        self.ledger.add(256 + 4 * length)
        self.ids.add(key)
        self.id_bytes += length

    def finish(self) -> NormalizedDatasetManifest:
        if self.manifest is None:
            raise failure("missing_terminal")
        return self.manifest
