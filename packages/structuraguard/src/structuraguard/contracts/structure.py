"""Bounded observations profiler: только данные, без исполняемых правил."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PhysicalObjectKind,
    PhysicalSourceRef,
    PositiveInt,
)

type ProfileText = Annotated[StrictStr, Field(max_length=4096)]
type ProfileRefs = Annotated[
    tuple[PhysicalSourceRef, ...], Field(min_length=1, max_length=8)
]
type PrimitiveHint = Literal[
    "string",
    "integer",
    "decimal",
    "boolean",
    "null",
    "date",
    "timestamp",
    "identifier",
    "bytes",
]


class StructuralProfilingOptions(FrozenContract):
    """Неизменяемые бюджеты одного прохода; sample prefix выбирается без RNG.

    Бюджет bytes покрывает UTF-8 payload retained samples с запасом на их
    координаты. Отдельные пределы ограничивают input batch и derived output.
    Переполнение выборки отмечает coverage, operational limits прерывают run.
    max_sample_items/max_sample_bytes общие для всех семейств; tail_rows задаёт
    хвостовое окно таблиц, 0 его отключает. Поля содержат defaults и hard caps;
    неверные значения дают Pydantic ValidationError. Создание options без I/O.
    """

    max_sample_items: Annotated[PositiveInt, Field(le=1000)] = 256
    max_sample_bytes: Annotated[PositiveInt, Field(le=1_048_576)] = 65_536
    tail_rows: Annotated[NonNegativeInt, Field(le=128)] = 8
    max_value_chars: Annotated[PositiveInt, Field(le=4096)] = 1024
    max_structures: Annotated[PositiveInt, Field(le=256)] = 64
    max_columns: Annotated[PositiveInt, Field(le=1024)] = 500
    max_depth: Annotated[PositiveInt, Field(le=30)] = 30
    max_patterns: Annotated[PositiveInt, Field(le=1024)] = 128
    max_observations: Annotated[PositiveInt, Field(le=2048)] = 512
    max_candidates: Annotated[PositiveInt, Field(le=32)] = 32
    max_profile_bytes: Annotated[PositiveInt, Field(le=4_194_304)] = 1_048_576
    max_batch_bytes: Annotated[PositiveInt, Field(le=33_554_432)] = 8_388_608
    max_batch_items: Annotated[PositiveInt, Field(le=1_000_000)] = 100_000
    max_batches: Annotated[PositiveInt, Field(le=10_000)] = 10_000
    max_total_items: Annotated[PositiveInt, Field(le=10_000_000)] = 2_000_000
    max_physical_objects: Annotated[PositiveInt, Field(le=10_000_000)] = 2_000_000
    max_processing_seconds: Annotated[PositiveInt, Field(le=300)] = 300


class ProfileCoverage(FrozenContract):
    """Размер и ограничения выборки, отдельно от завершённости extraction."""

    policy: Literal["physical_prefix_tail_v1"] = "physical_prefix_tail_v1"
    options_fingerprint: FingerprintStr
    seen_items: NonNegativeInt
    sampled_items: NonNegativeInt
    sampled_bytes: NonNegativeInt
    skipped_items: NonNegativeInt
    complete: StrictBool
    reasons: Annotated[tuple[IdentifierStr, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def _validate_counts(self) -> Self:
        if self.sampled_items + self.skipped_items != self.seen_items:
            raise ValueError("Sample counts не совпадают с examined items")
        if self.complete != (not self.skipped_items and not self.reasons):
            raise ValueError("Complete coverage не совпадает с ограничениями")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("Coverage reasons должны быть уникальны")
        return self


class ProfileCount(FrozenContract):
    """Число вхождений имени либо безопасной primitive type hint."""

    name: ProfileText
    count: PositiveInt


class ProfilePathStep(FrozenContract):
    """Структурная координата; item обозначает повтор, но не executable wildcard."""

    kind: Literal["key", "item", "element", "root"]
    name: ProfileText = ""
    occurrence: NonNegativeInt = 0


type ProfilePath = Annotated[tuple[ProfilePathStep, ...], Field(max_length=30)]
type ProfileCounts = Annotated[tuple[ProfileCount, ...], Field(max_length=1024)]


class _Observation(FrozenContract):
    source_refs: ProfileRefs

    @model_validator(mode="after")
    def _unique_refs(self) -> Self:
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValueError("Observation references должны быть уникальны")
        if len({ref.extraction_id for ref in self.source_refs}) != 1:
            raise ValueError("Observation смешивает extraction runs")
        if (
            isinstance(self, TabularObservation)
            and self.source_refs[0].kind is not PhysicalObjectKind.TABLE
        ):
            raise ValueError("Tabular observation требует table anchor")
        if isinstance(self, TreeObservation) and any(
            ref.kind is not PhysicalObjectKind.TREE_NODE for ref in self.source_refs
        ):
            raise ValueError("Tree observation требует node refs")
        if isinstance(self, TextObservation) and any(
            ref.kind not in {PhysicalObjectKind.LINE, PhysicalObjectKind.BLOCK}
            for ref in self.source_refs
        ):
            raise ValueError("Text observation требует line/block refs")
        if isinstance(self, DocumentObservation):
            expected = (
                PhysicalObjectKind.TABLE
                if self.role == "table"
                else PhysicalObjectKind.BLOCK
            )
            if any(ref.kind is not expected for ref in self.source_refs):
                raise ValueError("Document observation содержит неверный ref kind")
        return self


class TabularObservation(_Observation):
    """Физические row indices включают обе границы; role остаётся гипотезой."""

    kind: Literal["tabular_profile"] = "tabular_profile"
    role: Literal[
        "shape",
        "header",
        "data",
        "repeated_header",
        "empty",
        "meta",
        "footer",
        "summary",
        "merged",
    ]
    table_id: IdentifierStr
    row_start: NonNegativeInt
    row_end: NonNegativeInt
    row_count: NonNegativeInt
    column_count: NonNegativeInt = 0
    ragged_rows: NonNegativeInt = 0
    widths: ProfileCounts = ()
    merged_ranges: Annotated[tuple[ProfileText, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.row_end < self.row_start or self.ragged_rows > self.row_count:
            raise ValueError("Некорректные размеры tabular observation")
        return self


class TreeObservation(_Observation):
    """Распределения относятся к sampled nodes, а не к ненаблюдаемому хвосту."""

    kind: Literal["tree_profile"] = "tree_profile"
    role: Literal["path", "record_root", "collection"]
    path: ProfilePath
    parent_path: ProfilePath | None = None
    occurrences: PositiveInt
    object_count: NonNegativeInt = 0
    array_count: NonNegativeInt = 0
    scalar_count: NonNegativeInt = 0
    keys: ProfileCounts = ()
    types: ProfileCounts = ()

    @property
    def key_counts(self) -> Mapping[str, int]:
        """Дать read-only представление sampled key frequencies."""
        return MappingProxyType({entry.name: entry.count for entry in self.keys})


class FieldObservation(_Observation):
    """Кандидат имени и распределение lexical типов без изменения raw value."""

    kind: Literal["field_profile"] = "field_profile"
    scope: IdentifierStr
    path: ProfilePath = ()
    column_index: NonNegativeInt | None = None
    suggested_name: IdentifierStr
    raw_label: ProfileText | None = None
    sampled_count: PositiveInt
    types: ProfileCounts

    @property
    def type_counts(self) -> Mapping[str, int]:
        """Дать read-only представление primitive type frequencies."""
        return MappingProxyType({entry.name: entry.count for entry in self.types})


class TextObservation(_Observation):
    """Bounded token signature, не регулярное выражение и не ParseRule."""

    kind: Literal["text_profile"] = "text_profile"
    role: Literal[
        "line_boundary",
        "block_boundary",
        "line_shape",
        "template",
        "key_value",
        "multiline",
    ]
    signature: Annotated[tuple[ProfileText, ...], Field(max_length=64)] = ()
    occurrences: PositiveInt
    line_start: PositiveInt
    line_end: PositiveInt
    keys: Annotated[tuple[ProfileText, ...], Field(max_length=64)] = ()
    timestamp_count: NonNegativeInt = 0
    level_count: NonNegativeInt = 0

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("Некорректный line span")
        return self


class DocumentObservation(_Observation):
    """Наблюдения по physical order блоков и таблиц документа."""

    kind: Literal["document_profile"] = "document_profile"
    role: Literal["heading", "section", "key_value", "table", "repeated_group"]
    block_start: NonNegativeInt | None = None
    block_end: NonNegativeInt | None = None
    occurrences: PositiveInt = 1
    label: ProfileText | None = None
    block_kinds: Annotated[tuple[ProfileText, ...], Field(max_length=64)] = ()
    table_id: IdentifierStr | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.role == "table":
            if (
                self.table_id is None
                or self.block_start is not None
                or self.block_end is not None
            ):
                raise ValueError(
                    "Table candidate требует table_id, без выдуманных block offsets"
                )
            return self
        if (
            self.block_start is None
            or self.block_end is None
            or self.block_end < self.block_start
        ):
            raise ValueError("Некорректный block range")
        return self


type ProfilerObservation = (
    TabularObservation
    | TreeObservation
    | FieldObservation
    | TextObservation
    | DocumentObservation
)
