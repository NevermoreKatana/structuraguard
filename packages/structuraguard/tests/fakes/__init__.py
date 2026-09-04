"""Переиспользуемые управляемые fake adapters для tests."""

from tests.fakes.parsers import (
    FakeParser,
    FakeSourceReader,
    ProbeFactory,
    valid_extracted_batches,
)

__all__ = (
    "FakeParser",
    "FakeSourceReader",
    "ProbeFactory",
    "valid_extracted_batches",
)
