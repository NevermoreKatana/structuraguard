"""Чистые операции над domain contracts без I/O."""

from structuraguard.domain.canonical import canonical_json, sha256_fingerprint
from structuraguard.domain.lineage import (
    batch_sequence_is_contiguous,
    references_belong_to_extraction,
    unresolved_physical_refs,
)

__all__ = (
    "batch_sequence_is_contiguous",
    "canonical_json",
    "references_belong_to_extraction",
    "sha256_fingerprint",
    "unresolved_physical_refs",
)
