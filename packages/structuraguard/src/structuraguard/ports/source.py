"""Runtime-контракты ограниченного чтения source snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")


def _validate_fingerprint(value: str) -> None:
    digest = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(digest) != 64
        or any(character not in _LOWER_HEX_DIGITS for character in digest)
    ):
        raise ValueError("source_fingerprint должен иметь canonical SHA-256 form")


def _validate_positive_int(value: int, *, field: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} должен быть положительным int")


@runtime_checkable
class SourceReader(Protocol):
    """Читает только fingerprint-bound snapshot без раскрытия пути источника."""

    @property
    def source_fingerprint(self) -> str:
        """Вернуть canonical fingerprint неизменяемого snapshot."""

    async def read(self, *, offset: int, size: int) -> bytes:
        """Вернуть не более ``size`` байт snapshot начиная с ``offset``."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProbeContext:
    """Ограниченный runtime-контекст определения пригодности parser."""

    reader: SourceReader = field(compare=False, repr=False)
    source_fingerprint: str
    max_probe_bytes: int

    def __post_init__(self) -> None:
        _validate_fingerprint(self.source_fingerprint)
        _validate_positive_int(self.max_probe_bytes, field="max_probe_bytes")
        if self.reader.source_fingerprint != self.source_fingerprint:
            raise ValueError("reader не связан с fingerprint текущего источника")


@dataclass(frozen=True, slots=True, kw_only=True)
class ParseContext:
    """Runtime-контекст technical parsing с конечными resource limits."""

    reader: SourceReader = field(compare=False, repr=False)
    source_fingerprint: str
    max_bytes: int
    max_records: int
    max_nesting_depth: int

    def __post_init__(self) -> None:
        _validate_fingerprint(self.source_fingerprint)
        if self.reader.source_fingerprint != self.source_fingerprint:
            raise ValueError("reader не связан с fingerprint текущего источника")
        _validate_positive_int(self.max_bytes, field="max_bytes")
        _validate_positive_int(self.max_records, field="max_records")
        _validate_positive_int(self.max_nesting_depth, field="max_nesting_depth")
