"""Канонические fingerprints недоверенного physical extraction output."""

from __future__ import annotations

from typing import cast

from structuraguard.contracts._base import CanonicalInput, canonical_sha256_value
from structuraguard.contracts.source import ExtractedBatch, ExtractedDatasetManifest

_BATCH_EXCLUDED_FIELDS = frozenset({"batch_fingerprint", "manifest"})
_MANIFEST_EXCLUDED_FIELDS = frozenset({"extraction_fingerprint"})


def batch_fingerprint(batch: ExtractedBatch) -> str:
    """Вычислить hash всех batch fields кроме self-reference и manifest."""

    payload = cast(
        CanonicalInput,
        batch.model_dump(
            mode="python",
            round_trip=True,
            exclude=set(_BATCH_EXCLUDED_FIELDS),
            warnings="error",
        ),
    )
    return canonical_sha256_value(payload)


def manifest_fingerprint(manifest: ExtractedDatasetManifest) -> str:
    """Вычислить hash полного manifest кроме его собственного fingerprint."""

    return canonical_sha256_value(
        manifest,
        exclude_top_level=_MANIFEST_EXCLUDED_FIELDS,
    )


__all__ = ("batch_fingerprint", "manifest_fingerprint")
