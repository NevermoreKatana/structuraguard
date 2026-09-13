"""Проекция exact normalized values; без coercion, SQL и доверия внешнему report."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

from pydantic import ValidationError as ModelError

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.loading import DryRunRequest
from structuraguard.contracts.mapping import FieldMapping
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
)
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.record_validation import (
    ValidationCell,
    ValidationDataset,
    ValidationRecord,
)
from structuraguard.exceptions import LoadError, MappingError
from structuraguard.mapping._inputs import bounded_size
from structuraguard.profiling import NormalizedDataProfiler


def failure(code: str) -> LoadError:
    return LoadError(error_code=code, message=code)


def checked[T: FrozenContract](value: T, cls: type[T], max_bytes: int) -> T:
    try:
        if type(value) is not cls:
            raise failure("DRY_RUN_INPUT_INVALID")
        bounded_size(value, max_bytes)
        return cls.model_validate(value.model_dump(mode="python", warnings=False))
    except MappingError:
        raise failure("SECURITY_LIMIT_EXCEEDED") from None
    except (ModelError, ValueError, TypeError, AttributeError, RecursionError):
        raise failure("DRY_RUN_INPUT_INVALID") from None


@dataclass(frozen=True)
class Origin:
    record_id: str
    entity_id: str
    value_ids: tuple[str, ...]


@dataclass(frozen=True)
class Prepared:
    request: DryRunRequest
    manifest: NormalizedDatasetManifest
    profile: NormalizedDataProfile
    data: ValidationDataset
    origins: dict[str, Origin]


async def prepare(
    request: DryRunRequest, *, max_bytes: int = 4194304, max_records: int = 1000
) -> Prepared:
    """Перепроверить EOF/hashes и построить target rows до DB I/O.

    Несколько entity типов в одной target row требуют отдельного правила join и
    отклоняются. Copy values поддержаны; replay преобразований относится к M12
    coordinator и не подменяется hash или внешним ValidationReport.
    """
    request = checked(request, DryRunRequest, max_bytes)
    batches = request.batches
    manifest = batches[-1].manifest
    if manifest is None or manifest.schema_version not in ("1.1.0", "1.2.0"):
        raise failure("DRY_RUN_INPUT_INVALID")
    try:
        manifest.validate_batches(batches)
    except ValueError:
        raise failure("DRY_RUN_INPUT_INVALID") from None
    if manifest.record_count > max_records:
        raise failure("SECURITY_LIMIT_EXCEEDED")
    mappings: dict[str, list[FieldMapping]] = {}
    for mapping in request.mapping.mappings:
        mappings.setdefault(mapping.target.table_id, []).append(mapping)
    if any(
        len({m.source.entity_type for m in group}) != 1 for group in mappings.values()
    ):
        raise failure("DRY_RUN_PROJECTION_AMBIGUOUS")
    origins: dict[str, Origin] = {}
    rows: list[ValidationRecord] = []
    for batch in batches:
        for record in batch.records:
            matched = False
            for entity in record.entities:
                values = {v.field_name: v for v in entity.values}
                for tid, group in sorted(mappings.items()):
                    if group[0].source.entity_type != entity.entity_type:
                        continue
                    matched = True
                    cells: list[ValidationCell] = []
                    value_ids: list[str] = []
                    for m in group:
                        value = values.get(m.source.field_name)
                        if value is None:
                            continue
                        if (
                            value.issue_codes
                            or value.transformations
                            or value.raw_value != value.normalized_value
                        ):
                            raise failure("DRY_RUN_PROVENANCE_UNVERIFIED")
                        cells.append(
                            ValidationCell(
                                field_id=m.target.column_id,
                                value=value.normalized_value,
                            )
                        )
                        value_ids.append(value.value_id)
                    uid = canonical_sha256_value(
                        (record.record_id, entity.entity_id, tid)
                    )
                    origins[uid] = Origin(
                        record.record_id, entity.entity_id, tuple(value_ids)
                    )
                    rows.append(
                        ValidationRecord(
                            record_id=uid, collection_id=tid, values=tuple(cells)
                        )
                    )
                    if len(rows) > max_records:
                        raise failure("SECURITY_LIMIT_EXCEEDED")
            if not matched:
                raise failure("DRY_RUN_RECORD_UNMAPPED")

    async def stream() -> AsyncIterator[NormalizedBatch]:
        for batch in batches:
            yield batch

    profile = await NormalizedDataProfiler().profile(stream())
    return Prepared(
        request, manifest, profile, ValidationDataset(records=tuple(rows)), origins
    )
