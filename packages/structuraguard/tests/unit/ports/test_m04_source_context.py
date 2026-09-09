"""Additive runtime limits для parser adapters milestone M4."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass

import pytest

from structuraguard.ports.source import BatchOptions, ParseContext

SOURCE_FINGERPRINT = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class _Reader:
    source_fingerprint: str = SOURCE_FINGERPRINT

    async def read(self, *, offset: int, size: int) -> bytes:
        del offset, size
        return b""


def _context(**changes: object) -> ParseContext:
    values: dict[str, object] = {
        "reader": _Reader(),
        "source_fingerprint": SOURCE_FINGERPRINT,
        "max_bytes": 4_096,
        "max_records": 100,
        "max_nesting_depth": 16,
    }
    values.update(changes)
    return ParseContext(**values)  # type: ignore[arg-type]


def test_batch_options_have_safe_frozen_defaults() -> None:
    options = BatchOptions()

    assert options.batch_size == 1_000
    assert options.max_batches == 10_000
    with pytest.raises(FrozenInstanceError):
        options.batch_size = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("batch_size", 0),
        ("batch_size", -1),
        ("batch_size", True),
        ("batch_size", 1.0),
        ("max_batches", 0),
        ("max_batches", -1),
        ("max_batches", True),
        ("max_batches", 1.0),
    ),
)
def test_batch_options_require_exact_positive_integers(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {}
    values[field] = value
    with pytest.raises(ValueError):
        BatchOptions(**values)  # type: ignore[arg-type]


def test_batch_options_accept_exact_hard_caps() -> None:
    options = BatchOptions(batch_size=1_000_000, max_batches=10_000)

    assert options.batch_size == 1_000_000
    assert options.max_batches == 10_000


@pytest.mark.parametrize(
    ("field", "value"),
    (("batch_size", 1_000_001), ("max_batches", 10_001)),
)
def test_batch_options_reject_values_above_hard_caps(
    field: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match="hard cap"):
        BatchOptions(**{field: value})


def test_parse_context_additive_defaults_preserve_legacy_construction() -> None:
    context = _context()

    assert context.batch_options == BatchOptions()
    assert context.max_physical_objects == 2_000_000
    assert context.detected_encoding is None


def test_parse_context_accepts_explicit_batch_and_physical_object_limits() -> None:
    options = BatchOptions(batch_size=64, max_batches=256)
    context = _context(
        batch_options=options,
        max_physical_objects=10_000,
    )

    assert context.batch_options is options
    assert context.max_physical_objects == 10_000
    with pytest.raises(FrozenInstanceError):
        context.max_physical_objects = 1  # type: ignore[misc]


def test_parse_context_requires_batch_options_instance() -> None:
    with pytest.raises(ValueError):
        _context(batch_options={"batch_size": 64, "max_batches": 256})


def test_parse_context_accepts_safe_frozen_detected_encoding() -> None:
    context = _context(detected_encoding="cp1251")

    assert context.detected_encoding == "cp1251"
    with pytest.raises(FrozenInstanceError):
        context.detected_encoding = "utf-8"  # type: ignore[misc]


@pytest.mark.parametrize(
    "value",
    (
        "",
        "utf 8",
        "../utf-8",
        "utf/8",
        "utf-8\n",
        "кириллица",
        "a" * 65,
        True,
        1,
    ),
)
def test_parse_context_rejects_unsafe_detected_encoding(value: object) -> None:
    with pytest.raises(ValueError):
        _context(detected_encoding=value)


@pytest.mark.parametrize("value", (0, -1, True, 1.0))
def test_parse_context_rejects_invalid_physical_object_limit(value: object) -> None:
    with pytest.raises(ValueError):
        _context(max_physical_objects=value)


def test_parse_context_physical_object_limit_has_exact_hard_cap() -> None:
    assert _context(max_physical_objects=10_000_000).max_physical_objects == 10_000_000

    with pytest.raises(ValueError, match="hard cap"):
        _context(max_physical_objects=10_000_001)
