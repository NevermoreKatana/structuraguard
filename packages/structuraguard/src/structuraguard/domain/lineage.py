"""Чистые проверки разрешимости provenance references."""

from __future__ import annotations

from structuraguard.contracts.common import BatchFingerprint, PhysicalSourceRef
from structuraguard.contracts.source import ExtractedSourceIndex


def batch_sequence_is_contiguous(batches: tuple[BatchFingerprint, ...]) -> bool:
    """Проверить точную последовательность индексов от нуля без пропусков."""

    return tuple(batch.batch_index for batch in batches) == tuple(range(len(batches)))


def unresolved_physical_refs(
    references: tuple[PhysicalSourceRef, ...],
    source_index: ExtractedSourceIndex,
) -> tuple[PhysicalSourceRef, ...]:
    """Вернуть ссылки, отсутствующие в проверенном source index."""

    available = frozenset(source_index.refs)
    return tuple(reference for reference in references if reference not in available)


def references_belong_to_extraction(
    references: tuple[PhysicalSourceRef, ...],
    extraction_id: str,
) -> bool:
    """Проверить, что provenance не смешивает extraction runs."""

    return all(reference.extraction_id == extraction_id for reference in references)
