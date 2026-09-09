"""Runtime-контракты ограниченного чтения source snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")
_MAX_BATCH_SIZE = 1_000_000
_MAX_BATCHES = 10_000
_MAX_PHYSICAL_OBJECTS = 10_000_000


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


def _validate_bounded_positive_int(
    value: int,
    *,
    field: str,
    maximum: int,
) -> None:
    _validate_positive_int(value, field=field)
    if value > maximum:
        raise ValueError(f"{field} не может превышать hard cap {maximum}")


def _validate_encoding_name(value: str | None) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or any(
            not (character.isascii() and (character.isalnum() or character in "._-"))
            for character in value
        )
    ):
        raise ValueError("detected_encoding должен быть безопасным именем codec")


@dataclass(frozen=True, slots=True, kw_only=True)
class BatchOptions:
    """Конечные пределы формирования physical batches.

    ``batch_size`` задаёт целевое число логических единиц формата. Адаптер не
    обязан разрезать неделимый physical block, чтобы точно попасть в предел.
    Единица зависит от adapter: например, TXT lines, CSV rows или PDF pages.
    ``max_batches`` ограничивает всё извлечение, а не только один segment.

    Raises:
        ValueError: Предел не является положительным int или превышает hard cap
            (1 000 000 для batch_size, 10 000 для max_batches).
    """

    batch_size: int = 1_000
    max_batches: int = 10_000

    def __post_init__(self) -> None:
        _validate_bounded_positive_int(
            self.batch_size,
            field="batch_size",
            maximum=_MAX_BATCH_SIZE,
        )
        _validate_bounded_positive_int(
            self.max_batches,
            field="max_batches",
            maximum=_MAX_BATCHES,
        )


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
    """Runtime-контекст technical parsing с конечными resource limits.

    ``reader`` читает неизменяемый snapshot с тем же ``source_fingerprint``.
    ``max_bytes``, ``max_records`` и ``max_nesting_depth`` задают общие бюджеты;
    более строгие format-specific limits adapter продолжают действовать.
    ``max_physical_objects`` ограничивает физические объекты, не бизнес-записи.
    ``batch_options`` задаёт целевой размер и максимальное число batches.
    ``detected_encoding`` переносится SelectedParser из проверенного probe;
    context сам не обнаруживает и не проверяет содержимое кодировки.

    Raises:
        ValueError: Предел, fingerprint, тип batch_options или имя codec не
            соответствуют контракту либо reader связан с другим fingerprint.

    Context не создаёт snapshot, не является sandbox и не задаёт общий timeout.
    """

    reader: SourceReader = field(compare=False, repr=False)
    source_fingerprint: str
    max_bytes: int
    max_records: int
    max_nesting_depth: int
    batch_options: BatchOptions = field(default_factory=BatchOptions)
    max_physical_objects: int = 2_000_000
    detected_encoding: str | None = None

    def __post_init__(self) -> None:
        _validate_fingerprint(self.source_fingerprint)
        if self.reader.source_fingerprint != self.source_fingerprint:
            raise ValueError("reader не связан с fingerprint текущего источника")
        _validate_positive_int(self.max_bytes, field="max_bytes")
        _validate_positive_int(self.max_records, field="max_records")
        _validate_positive_int(self.max_nesting_depth, field="max_nesting_depth")
        if type(self.batch_options) is not BatchOptions:
            raise ValueError("batch_options должен быть экземпляром BatchOptions")
        _validate_bounded_positive_int(
            self.max_physical_objects,
            field="max_physical_objects",
            maximum=_MAX_PHYSICAL_OBJECTS,
        )
        _validate_encoding_name(self.detected_encoding)
