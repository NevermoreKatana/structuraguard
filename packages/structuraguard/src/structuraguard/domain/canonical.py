"""Чистое canonical fingerprinting для проверенных контрактов."""

from __future__ import annotations

from structuraguard.contracts._base import (
    CanonicalInput,
    canonical_json_value,
    canonical_sha256_value,
)


def canonical_json(value: CanonicalInput) -> str:
    """Вернуть стабильный JSON независимо от порядка ключей mapping."""

    return canonical_json_value(value)


def sha256_fingerprint(value: CanonicalInput) -> str:
    """Вычислить typed SHA-256, исключив self-referential fingerprint поля."""

    return canonical_sha256_value(
        value,
        exclude_top_level=frozenset({"fingerprint"}),
    )
