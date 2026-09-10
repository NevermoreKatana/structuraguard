"""Чистые операции над domain contracts без I/O."""

from structuraguard.domain.canonical import canonical_json, sha256_fingerprint
from structuraguard.domain.database_fingerprint import (
    canonical_database_catalog,
    database_fingerprint,
    verify_database_fingerprint,
)
from structuraguard.domain.database_graph import build_dependency_graph
from structuraguard.domain.lineage import (
    batch_sequence_is_contiguous,
    references_belong_to_extraction,
    unresolved_physical_refs,
)

__all__ = (
    "batch_sequence_is_contiguous",
    "build_dependency_graph",
    "canonical_database_catalog",
    "canonical_json",
    "database_fingerprint",
    "references_belong_to_extraction",
    "sha256_fingerprint",
    "unresolved_physical_refs",
    "verify_database_fingerprint",
)
