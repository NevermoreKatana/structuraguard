"""Физическая модель источника после технического parsing."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    WrapValidator,
    field_validator,
    model_validator,
)

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    BatchFingerprint,
    ConfidenceDecimal,
    FingerprintStr,
    FiniteFloat,
    IdentifierStr,
    NonNegativeInt,
    ParserIdentifierStr,
    PhysicalObjectKind,
    PhysicalSourceRef,
    PositiveInt,
    ProducerMetadata,
    RawScalar,
    SchemaVersionStr,
    SourceArtifactRef,
    VersionStr,
    _redact_union_validation_input,
    _validate_generated_extraction_id,
    _validate_generated_physical_local_id,
)

_SUPPORTED_EXTRACTION_SCHEMA_VERSIONS = frozenset({"1.0.0", "1.1.0"})
_MAX_JSON_POINTER_SEGMENTS = 4_096
_MAX_RAW_TREE_NAME_CHARS = 1_048_576
type _PhysicalRefKey = tuple[PhysicalObjectKind, str]


class SourceArtifact(FrozenContract):
    """Сериализуемое описание fingerprint-bound snapshot источника."""

    artifact_id: IdentifierStr
    schema_version: SchemaVersionStr = "1.0.0"
    display_name: IdentifierStr
    media_type: IdentifierStr
    size_bytes: NonNegativeInt
    source_fingerprint: FingerprintStr

    @property
    def ref(self) -> SourceArtifactRef:
        """Вернуть компактную downstream-ссылку на тот же snapshot."""

        return SourceArtifactRef(
            artifact_id=self.artifact_id,
            source_fingerprint=self.source_fingerprint,
        )


class ProbeSignalKind(StrEnum):
    """Закрытые виды сигналов технического определения формата.

    ``SIGNATURE``, ``CONTENT_MEDIA_TYPE`` и ``INTERNAL_STRUCTURE`` являются
    сильными сигналами. ``DECLARED_MEDIA_TYPE`` и ``EXTENSION`` используются
    как advisory metadata и сами по себе не подтверждают формат.
    """

    SIGNATURE = "signature"
    CONTENT_MEDIA_TYPE = "content_media_type"
    INTERNAL_STRUCTURE = "internal_structure"
    DECLARED_MEDIA_TYPE = "declared_media_type"
    EXTENSION = "extension"


class ProbeSignalOutcome(StrEnum):
    """Исход сравнения одного сигнала с форматом parser."""

    MATCH = "match"
    MISMATCH = "mismatch"
    INCONCLUSIVE = "inconclusive"


class ProbeSignal(FrozenContract):
    """Один ограниченный сигнал без raw source и произвольных метаданных.

    Args:
        kind: Вид проверенного сигнала.
        outcome: Совпадение, несовпадение или отсутствие достаточных данных.

    Raises:
        pydantic.ValidationError: Если передан неизвестный вид или исход.
    """

    kind: ProbeSignalKind
    outcome: ProbeSignalOutcome


class ProbeResult(FrozenContract):
    """Результат безопасного технического определения формата.

    Args:
        source: Fingerprint-bound ссылка на проверенный source snapshot.
        adapter_id: Идентификатор выполнившего probe parser adapter.
        adapter_version: Версия parser adapter.
        supported: Подтвердил ли parser поддержку формата.
        confidence: Оценка уверенности от 0 до 1.
        detected_media_type: Media type, определённый по содержимому.
        detected_encoding: Определённая кодировка, если она применима.
        warnings: Уникальные machine-readable warning codes.
        format_id: Канонический идентификатор подтверждённого формата.
        signals: Не более пяти сигналов с уникальными ``kind``.

    Raises:
        pydantic.ValidationError: Если нарушены типы, пределы или инварианты DTO.

    Для ``supported=True`` DTO требует ``detected_media_type``. Registry перед
    выбором дополнительно требует положительный ``confidence``, ``format_id``,
    сильный совпавший сигнал и отсутствие сильного несовпадения. Несовпадение
    заявленного MIME или extension отражается warning code, а неоднозначные
    сильные сигналы приводят к typed ``ParserError``.
    """

    source: SourceArtifactRef
    adapter_id: IdentifierStr
    adapter_version: VersionStr
    supported: StrictBool
    confidence: ConfidenceDecimal
    detected_media_type: IdentifierStr | None = None
    detected_encoding: IdentifierStr | None = None
    warnings: tuple[IdentifierStr, ...] = ()
    format_id: ParserIdentifierStr | None = None
    signals: Annotated[tuple[ProbeSignal, ...], Field(max_length=5)] = ()

    @model_validator(mode="after")
    def _require_detection_for_supported_source(self) -> Self:
        if self.supported and self.detected_media_type is None:
            raise ValueError("Поддерживаемый источник должен иметь detected_media_type")
        if len(self.warnings) != len(set(self.warnings)):
            raise ValueError("Probe warnings должны быть уникальны")
        signal_kinds = tuple(signal.kind for signal in self.signals)
        if len(signal_kinds) != len(set(signal_kinds)):
            raise ValueError("Probe signal kinds должны быть уникальны")
        return self


class LineRangeLocation(FrozenContract):
    """Диапазон строк исходного snapshot, нумерация начинается с единицы."""

    kind: Literal["line_range"] = "line_range"
    source: SourceArtifactRef
    line_start: PositiveInt
    line_end: PositiveInt
    column_start: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    column_end: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end не может предшествовать line_start")
        if (self.column_start is None) != (self.column_end is None):
            raise ValueError("column_start и column_end указываются вместе")
        if (
            self.line_start == self.line_end
            and self.column_start is not None
            and self.column_end is not None
            and self.column_end < self.column_start
        ):
            raise ValueError("column_end не может предшествовать column_start")
        return self


class TabularCellLocation(FrozenContract):
    """Ячейка физической таблицы с нулевыми индексами."""

    kind: Literal["tabular_cell"] = "tabular_cell"
    source: SourceArtifactRef
    table_id: IdentifierStr
    row_index: NonNegativeInt
    column_index: NonNegativeInt


class SheetCellLocation(FrozenContract):
    """Ячейка листа с нулевыми физическими индексами."""

    kind: Literal["sheet_cell"] = "sheet_cell"
    source: SourceArtifactRef
    sheet_name: Annotated[StrictStr, StringConstraints(min_length=1, max_length=255)]
    row_index: NonNegativeInt
    column_index: NonNegativeInt


class JsonPointerLocation(FrozenContract):
    """RFC 6901 JSON Pointer и optional physical position внутри snapshot."""

    kind: Literal["json_pointer"] = "json_pointer"
    source: SourceArtifactRef
    pointer: Annotated[StrictStr, StringConstraints(max_length=4096)]
    record_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    line_start: PositiveInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    line_end: PositiveInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    column_start: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    column_end: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    occurrence_path: (
        Annotated[
            tuple[NonNegativeInt | None, ...],
            Field(max_length=_MAX_JSON_POINTER_SEGMENTS),
        ]
        | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_pointer(self) -> Self:
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("JSON Pointer должен быть пустым или начинаться с '/'")
        position = 0
        while position < len(self.pointer):
            if self.pointer[position] != "~":
                position += 1
                continue
            if position + 1 >= len(self.pointer) or self.pointer[position + 1] not in {
                "0",
                "1",
            }:
                raise ValueError("JSON Pointer содержит некорректный escape")
            position += 2
        if self.occurrence_path is not None and len(self.occurrence_path) != (
            0 if not self.pointer else self.pointer.count("/")
        ):
            raise ValueError(
                "occurrence_path должен соответствовать сегментам JSON Pointer"
            )
        if (self.line_start is None) != (self.line_end is None):
            raise ValueError("line_start и line_end указываются вместе")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end не может предшествовать line_start")
        if (self.column_start is None) != (self.column_end is None):
            raise ValueError("column_start и column_end указываются вместе")
        if self.column_start is not None and self.line_start is None:
            raise ValueError("JSON columns требуют physical line range")
        if (
            self.line_start == self.line_end
            and self.column_start is not None
            and self.column_end is not None
            and self.column_end < self.column_start
        ):
            raise ValueError("column_end не может предшествовать column_start")
        return self


class XPathLocation(FrozenContract):
    """Ограниченная XPath-ссылка; исполнение выражения остаётся в parser adapter."""

    kind: Literal["xpath"] = "xpath"
    source: SourceArtifactRef
    xpath: Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096)]
    namespace_prefix: (
        Annotated[StrictStr, StringConstraints(max_length=4096)] | None
    ) = Field(
        default=None,
        exclude_if=lambda v: v is None,
    )


class CssSelectorLocation(FrozenContract):
    """CSS-селектор физического элемента source snapshot."""

    kind: Literal["css_selector"] = "css_selector"
    source: SourceArtifactRef
    selector: Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096)]
    node_index: NonNegativeInt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    line_number: PositiveInt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    column_number: NonNegativeInt | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _validate_position(self) -> Self:
        if self.column_number is not None and self.line_number is None:
            raise ValueError("CSS column_number требует line_number")
        return self


class BoundingBox(FrozenContract):
    """Нормализованный прямоугольник страницы в физических координатах."""

    x: Annotated[FiniteFloat, Field(ge=0)]
    y: Annotated[FiniteFloat, Field(ge=0)]
    width: Annotated[FiniteFloat, Field(gt=0)]
    height: Annotated[FiniteFloat, Field(gt=0)]


class DocumentBlockLocation(FrozenContract):
    """Блок документа с проверяемым page/block identity."""

    kind: Literal["document_block"] = "document_block"
    source: SourceArtifactRef
    block_id: IdentifierStr
    page_number: PositiveInt | None = None
    bounding_box: BoundingBox | None = None

    @model_validator(mode="after")
    def _bind_box_to_page(self) -> Self:
        if self.bounding_box is not None and self.page_number is None:
            raise ValueError("Bounding box требует проверяемый page_number")
        return self


class ExtensionMetadataEntry(FrozenContract):
    """Одна bounded scalar metadata-запись plugin location."""

    key: IdentifierStr
    value: (
        Annotated[StrictStr, StringConstraints(max_length=4096)]
        | StrictInt
        | FiniteFloat
        | StrictBool
        | None
    )

    @field_validator("value", mode="before")
    @classmethod
    def _require_exact_json_scalar(cls, value: object) -> object:
        if value is not None and type(value) not in {str, int, float, bool}:
            raise ValueError("Extension metadata принимает только JSON scalar")
        return value

    @model_validator(mode="after")
    def _reject_non_finite_number(self) -> Self:
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("Extension metadata number должен быть конечным")
        return self


# Совместимый public alias сохраняет один wire-контракт bounded metadata.
PhysicalMetadataEntry = ExtensionMetadataEntry


def _validate_physical_metadata(
    metadata: tuple[ExtensionMetadataEntry, ...],
) -> None:
    keys = tuple(entry.key for entry in metadata)
    if len(keys) != len(set(keys)):
        raise ValueError("Physical metadata keys должны быть уникальны")


class ExtensionLocation(FrozenContract):
    """Namespaced физическая location для plugin без mutable metadata."""

    kind: Literal["extension"] = "extension"
    source: SourceArtifactRef
    namespace: Annotated[
        StrictStr,
        StringConstraints(
            min_length=3,
            max_length=255,
            pattern=r"^[a-z][a-z0-9_.-]*:[a-z][a-z0-9_.-]*$",
        ),
    ]
    metadata: Annotated[tuple[ExtensionMetadataEntry, ...], Field(max_length=64)] = ()

    @model_validator(mode="after")
    def _validate_metadata_keys(self) -> Self:
        keys = tuple(entry.key for entry in self.metadata)
        if len(keys) != len(set(keys)):
            raise ValueError("Extension metadata keys должны быть уникальны")
        return self


type SourceLocation = Annotated[
    LineRangeLocation
    | TabularCellLocation
    | SheetCellLocation
    | JsonPointerLocation
    | XPathLocation
    | CssSelectorLocation
    | DocumentBlockLocation
    | ExtensionLocation,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class ExtractedBlockKind(StrEnum):
    """Стабильные физические виды блоков."""

    LINE = "line"
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    LIST = "list"
    KEY_VALUE = "key_value"
    METADATA = "metadata"
    EXTENSION = "extension"


class PhysicalNodeKind(StrEnum):
    """Физический вид tree node без semantic интерпретации."""

    OBJECT = "object"
    ARRAY = "array"
    SCALAR = "scalar"
    DOCUMENT = "document"
    ELEMENT = "element"
    ATTRIBUTE = "attribute"
    TEXT = "text"
    COMMENT = "comment"
    DECLARATION = "declaration"
    PROCESSING_INSTRUCTION = "processing_instruction"
    NAMESPACE = "namespace"
    MAPPING = "mapping"
    SEQUENCE = "sequence"
    ALIAS = "alias"


class ExtractedValue(FrozenContract):
    """Raw value с технической подсказкой и точной physical location."""

    value_id: IdentifierStr | None = None
    raw_value: RawScalar
    location: SourceLocation
    technical_type_hint: IdentifierStr | None = None


class ExtractedLine(FrozenContract):
    """Физическая строка, не наделённая бизнес-смыслом."""

    line_id: IdentifierStr
    line_number: PositiveInt
    text: StrictStr
    location: SourceLocation
    values: tuple[ExtractedValue, ...] = ()
    metadata: Annotated[
        tuple[ExtensionMetadataEntry, ...],
        Field(max_length=64),
    ] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _validate_line_location(self) -> Self:
        if isinstance(self.location, LineRangeLocation) and not (
            self.location.line_start <= self.line_number <= self.location.line_end
        ):
            raise ValueError("line_number не входит в physical line range")
        _validate_physical_metadata(self.metadata)
        return self


class ExtractedBlock(FrozenContract):
    """Упорядоченный физический блок source document."""

    block_id: IdentifierStr
    kind: ExtractedBlockKind
    order: NonNegativeInt
    location: SourceLocation
    text: StrictStr | None = None
    values: tuple[ExtractedValue, ...] = ()
    lines: tuple[ExtractedLine, ...] = ()
    metadata: Annotated[
        tuple[ExtensionMetadataEntry, ...],
        Field(max_length=64),
    ] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _require_physical_content(self) -> Self:
        if self.text is None and not self.values and not self.lines:
            raise ValueError("ExtractedBlock требует raw text, values или lines")
        if (
            isinstance(self.location, DocumentBlockLocation)
            and self.location.block_id != self.block_id
        ):
            raise ValueError("block_id не совпадает с document block location")
        _validate_physical_metadata(self.metadata)
        return self


class ExtractedCell(FrozenContract):
    """Raw ячейка физической таблицы."""

    cell_id: IdentifierStr
    row_index: NonNegativeInt
    column_index: NonNegativeInt
    value: ExtractedValue
    metadata: Annotated[
        tuple[ExtensionMetadataEntry, ...],
        Field(max_length=32),
    ] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _validate_cell_location(self) -> Self:
        location = self.value.location
        if isinstance(location, TabularCellLocation | SheetCellLocation) and (
            location.row_index != self.row_index
            or location.column_index != self.column_index
        ):
            raise ValueError("cell coordinates не совпадают с physical location")
        _validate_physical_metadata(self.metadata)
        return self


class ExtractedTable(FrozenContract):
    """Физическая таблица с raw cells и без semantic field names."""

    table_id: IdentifierStr
    location: SourceLocation
    cells: tuple[ExtractedCell, ...] = ()
    segment_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    row_start_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    row_end_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    is_last_segment: StrictBool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    metadata: Annotated[
        tuple[ExtensionMetadataEntry, ...],
        Field(max_length=64),
    ] = Field(default=(), exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _validate_cells(self) -> Self:
        cell_ids = tuple(cell.cell_id for cell in self.cells)
        coordinates = tuple((cell.row_index, cell.column_index) for cell in self.cells)
        if len(cell_ids) != len(set(cell_ids)):
            raise ValueError("cell_id должны быть уникальны внутри таблицы")
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("Координаты cells должны быть уникальны внутри таблицы")
        if (
            isinstance(self.location, TabularCellLocation)
            and self.location.table_id != self.table_id
        ):
            raise ValueError("table_id не совпадает с physical table location")
        if any(
            isinstance(cell.value.location, TabularCellLocation)
            and cell.value.location.table_id != self.table_id
            for cell in self.cells
        ):
            raise ValueError("cell location относится к другой physical table")
        if isinstance(self.location, SheetCellLocation) and any(
            isinstance(cell.value.location, SheetCellLocation)
            and cell.value.location.sheet_name != self.location.sheet_name
            for cell in self.cells
        ):
            raise ValueError("cell location относится к другому physical sheet")
        segment_fields = (
            self.segment_index,
            self.row_start_index,
            self.row_end_index,
            self.is_last_segment,
        )
        if any(value is None for value in segment_fields) and any(
            value is not None for value in segment_fields
        ):
            raise ValueError("Поля table segment указываются вместе")
        if (
            self.row_start_index is not None
            and self.row_end_index is not None
            and self.row_end_index < self.row_start_index
        ):
            raise ValueError("row_end_index не может предшествовать row_start_index")
        if (
            self.row_start_index is not None
            and self.row_end_index is not None
            and any(
                not self.row_start_index <= cell.row_index <= self.row_end_index
                for cell in self.cells
            )
        ):
            raise ValueError("cell row_index находится вне table segment")
        _validate_physical_metadata(self.metadata)
        return self


class ExtractedTreeNode(FrozenContract):
    """Физический узел дерева без окончательной semantic интерпретации."""

    node_id: IdentifierStr
    parent_id: IdentifierStr | None = None
    name: IdentifierStr
    node_kind: PhysicalNodeKind | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    raw_name: (
        Annotated[
            StrictStr,
            StringConstraints(max_length=_MAX_RAW_TREE_NAME_CHARS),
        ]
        | None
    ) = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    order: NonNegativeInt
    location: SourceLocation
    value: ExtractedValue | None = None
    raw_lexeme: Annotated[StrictStr, StringConstraints(max_length=1048576)] | None = (
        Field(
            default=None,
            exclude_if=lambda v: v is None,
        )
    )
    metadata: Annotated[tuple[ExtensionMetadataEntry, ...], Field(max_length=64)] = (
        Field(
            default=(),
            exclude_if=lambda v: not v,
        )
    )
    tree_id: IdentifierStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    segment_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    child_start_index: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    child_count: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    is_last_segment: StrictBool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_node(self) -> Self:
        if self.parent_id == self.node_id:
            raise ValueError("Tree node не может ссылаться на себя как parent")
        _validate_physical_metadata(self.metadata)
        if (
            self.node_kind
            in {
                PhysicalNodeKind.SCALAR,
                PhysicalNodeKind.ATTRIBUTE,
                PhysicalNodeKind.TEXT,
                PhysicalNodeKind.COMMENT,
                PhysicalNodeKind.DECLARATION,
                PhysicalNodeKind.PROCESSING_INSTRUCTION,
                PhysicalNodeKind.NAMESPACE,
                PhysicalNodeKind.ALIAS,
            }
            and self.value is None
        ):
            raise ValueError("Scalar tree node требует raw value")
        if (
            self.node_kind
            in {
                PhysicalNodeKind.OBJECT,
                PhysicalNodeKind.ARRAY,
                PhysicalNodeKind.DOCUMENT,
                PhysicalNodeKind.ELEMENT,
                PhysicalNodeKind.MAPPING,
                PhysicalNodeKind.SEQUENCE,
            }
            and self.value is not None
        ):
            raise ValueError("Container tree node не может содержать scalar value")
        continuation_fields = (
            self.tree_id,
            self.segment_index,
            self.child_start_index,
            self.child_count,
            self.is_last_segment,
        )
        if any(value is None for value in continuation_fields) and any(
            value is not None for value in continuation_fields
        ):
            raise ValueError("Поля tree segment указываются вместе")
        if self.tree_id is not None:
            if self.parent_id is not None:
                raise ValueError("Tree continuation допустим только для root node")
            if self.node_kind not in {
                PhysicalNodeKind.OBJECT,
                PhysicalNodeKind.ARRAY,
                PhysicalNodeKind.ELEMENT,
                PhysicalNodeKind.DOCUMENT,
            }:
                raise ValueError("Tree continuation требует container root")
            if self.child_count == 0 and not self.is_last_segment:
                raise ValueError("Non-terminal tree segment должен содержать children")
        return self


class ExtractedSourceIndex(FrozenContract):
    """Bounded allowlist physical references, нужных следующим этапам.

    Индекс не перечисляет каждый физический объект: parser включает только
    references, которые разрешено использовать как sample/evidence/selectors.
    Полное существование allowlist проверяет ``validate_batches`` manifest.
    """

    refs: Annotated[tuple[PhysicalSourceRef, ...], Field(max_length=10_000)] = ()

    @model_validator(mode="after")
    def _validate_unique_refs(self) -> Self:
        if len(self.refs) != len(set(self.refs)):
            raise ValueError("Physical refs в source index должны быть уникальны")
        return self


class ExtractedBatchSummary(BatchFingerprint):
    """Сериализуемая lineage и размер одного physical batch."""

    source: SourceArtifactRef
    extraction_id: IdentifierStr
    parser_id: IdentifierStr
    parser_version: VersionStr
    physical_ref_count: NonNegativeInt
    record_count: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ExtractedDatasetManifest(FrozenContract):
    """Aggregate identity полного непрерывного technical extraction run."""

    schema_version: SchemaVersionStr = "1.0.0"
    source: SourceArtifactRef
    extraction_id: IdentifierStr
    parser_id: IdentifierStr
    parser_version: VersionStr
    producer: ProducerMetadata | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    parser_options_fingerprint: FingerprintStr | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    batches: tuple[ExtractedBatchSummary, ...]
    extraction_fingerprint: FingerprintStr
    source_index: ExtractedSourceIndex
    record_count: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_run(self) -> Self:
        if self.schema_version not in _SUPPORTED_EXTRACTION_SCHEMA_VERSIONS:
            raise ValueError("Неподдерживаемая версия physical extraction schema")
        if not self.batches:
            raise ValueError("Extraction manifest требует хотя бы один batch")
        indices = tuple(batch.batch_index for batch in self.batches)
        if indices != tuple(range(len(self.batches))):
            raise ValueError(
                "Batch indices должны образовывать непрерывную последовательность"
            )
        fingerprints = tuple(batch.batch_fingerprint for batch in self.batches)
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("Batch fingerprints в manifest должны быть уникальны")
        for batch in self.batches:
            if batch.source != self.source:
                raise ValueError("Batch summary относится к другому source")
            if batch.extraction_id != self.extraction_id:
                raise ValueError("Batch summary относится к другому extraction")
            if (
                batch.parser_id != self.parser_id
                or batch.parser_version != self.parser_version
            ):
                raise ValueError("Batch summary относится к другому parser")
        for reference in self.source_index.refs:
            if reference.extraction_id != self.extraction_id:
                raise ValueError("Physical ref принадлежит другому extraction run")
            if reference.batch_index not in indices:
                raise ValueError("Physical ref указывает на отсутствующий batch")
        indexed_counts: dict[int, int] = {}
        for reference in self.source_index.refs:
            indexed_counts[reference.batch_index] = (
                indexed_counts.get(reference.batch_index, 0) + 1
            )
        if any(
            indexed_counts.get(batch.batch_index, 0) > batch.physical_ref_count
            for batch in self.batches
        ):
            raise ValueError("Source index count превышает размер batch summary")
        summary_counts = tuple(batch.record_count for batch in self.batches)
        if self.schema_version == "1.1.0":
            if self.producer is None or self.parser_options_fingerprint is None:
                raise ValueError(
                    "Schema 1.1.0 требует producer и parser_options_fingerprint"
                )
            if (
                self.producer.component_id != self.parser_id
                or self.producer.component_version != self.parser_version
            ):
                raise ValueError("Producer identity не совпадает с parser identity")
            if self.record_count is None or any(
                count is None for count in summary_counts
            ):
                raise ValueError("Schema 1.1.0 требует record_count")
            if (
                sum(count for count in summary_counts if count is not None)
                != self.record_count
            ):
                raise ValueError("Manifest record_count не совпадает с batches")
        elif (
            self.record_count is not None
            or any(count is not None for count in summary_counts)
            or self.producer is not None
            or self.parser_options_fingerprint is not None
        ):
            raise ValueError(
                "Schema 1.0.0 не поддерживает поля physical extraction 1.1"
            )
        return self

    def validate_batch(self, batch: ExtractedBatch) -> None:
        """Сверить один полученный batch с manifest и bounded source index."""

        if self.schema_version not in _SUPPORTED_EXTRACTION_SCHEMA_VERSIONS:
            raise ValueError("Неподдерживаемая версия physical extraction schema")
        if batch.schema_version != self.schema_version:
            raise ValueError("Batch и manifest имеют разные schema versions")
        if batch.batch_index >= len(self.batches):
            raise ValueError("Batch отсутствует в manifest sequence")
        expected = self.batches[batch.batch_index]
        if batch.to_summary() != expected:
            raise ValueError("Batch summary не совпадает с manifest")
        expected_terminal = batch.batch_index == len(self.batches) - 1
        if batch.is_last != expected_terminal:
            raise ValueError("Terminal marker не совпадает с manifest sequence")
        if expected_terminal and batch.manifest != self:
            raise ValueError("Terminal batch содержит другой manifest")
        indexed_refs = {
            reference
            for reference in self.source_index.refs
            if reference.batch_index == batch.batch_index
        }
        if not batch._contains_physical_refs(indexed_refs):
            raise ValueError("Source index содержит отсутствующий physical ID")

    def validate_batches(self, batches: Iterable[ExtractedBatch]) -> None:
        """Проверить полную ordered sequence без повторной загрузки raw source."""

        batch_count = 0
        indexed_ref_offset = 0
        table_segments: dict[str, tuple[int, int, bool]] = {}
        tree_segments: dict[
            str,
            tuple[int, int, bool, PhysicalNodeKind],
        ] = {}
        for expected_index, batch in enumerate(batches):
            if expected_index >= len(self.batches):
                raise ValueError("Количество batches не совпадает с manifest")
            if batch.batch_index != expected_index:
                raise ValueError("Порядок batches не совпадает с manifest")
            self.validate_batch(batch)
            for table in batch.tables:
                if table.segment_index is None:
                    continue
                row_start = table.row_start_index
                row_end = table.row_end_index
                is_last = table.is_last_segment
                if row_start is None or row_end is None or is_last is None:
                    raise ValueError("Некорректный table segment contract")
                previous = table_segments.get(table.table_id)
                if previous is None:
                    if table.segment_index != 0:
                        raise ValueError(
                            "Table segment indices должны начинаться с нуля"
                        )
                else:
                    next_segment_index, next_row_index, closed = previous
                    if closed:
                        raise ValueError("Table segment следует после terminal segment")
                    if (
                        table.segment_index != next_segment_index
                        or row_start != next_row_index
                    ):
                        raise ValueError(
                            "Table segments должны образовывать непрерывную sequence"
                        )
                table_segments[table.table_id] = (
                    table.segment_index + 1,
                    row_end + 1,
                    is_last,
                )
            for node in batch.trees:
                if node.tree_id is None:
                    continue
                segment_index = node.segment_index
                child_start = node.child_start_index
                child_count = node.child_count
                is_last = node.is_last_segment
                node_kind = node.node_kind
                if (
                    segment_index is None
                    or child_start is None
                    or child_count is None
                    or is_last is None
                    or node_kind is None
                ):
                    raise ValueError("Некорректный tree segment contract")
                tree_previous = tree_segments.get(node.tree_id)
                if tree_previous is None:
                    if segment_index != 0 or child_start != 0:
                        raise ValueError(
                            "Tree segments должны начинаться с нулевых индексов"
                        )
                else:
                    next_segment_index, next_child_index, closed, previous_kind = (
                        tree_previous
                    )
                    if closed:
                        raise ValueError("Tree segment следует после terminal segment")
                    if previous_kind is not node_kind:
                        raise ValueError("Tree segments имеют разные container kinds")
                    if (
                        segment_index != next_segment_index
                        or child_start != next_child_index
                    ):
                        raise ValueError(
                            "Tree segments должны образовывать непрерывную sequence"
                        )
                tree_segments[node.tree_id] = (
                    segment_index + 1,
                    child_start + child_count,
                    is_last,
                    node_kind,
                )
            if self.schema_version == "1.1.0":
                next_offset = indexed_ref_offset + len(batch.indexed_refs)
                if (
                    next_offset > len(self.source_index.refs)
                    or batch.indexed_refs
                    != self.source_index.refs[indexed_ref_offset:next_offset]
                ):
                    raise ValueError(
                        "Source index не совпадает с progressive indexed refs"
                    )
                indexed_ref_offset = next_offset
            batch_count += 1
        if batch_count != len(self.batches):
            raise ValueError("Количество batches не совпадает с manifest")
        if self.schema_version == "1.1.0" and indexed_ref_offset != len(
            self.source_index.refs
        ):
            raise ValueError("Source index не совпадает с progressive indexed refs")
        if any(not closed for _, _, closed in table_segments.values()):
            raise ValueError("Segmented physical table требует terminal segment")
        if any(not closed for _, _, closed, _ in tree_segments.values()):
            raise ValueError("Segmented physical tree требует terminal segment")


class ExtractedBatch(FrozenContract):
    """Один batch физического результата technical parser."""

    schema_version: SchemaVersionStr = "1.0.0"
    extraction_id: IdentifierStr
    batch_index: NonNegativeInt
    source: SourceArtifactRef
    parser_id: IdentifierStr
    parser_version: VersionStr
    batch_fingerprint: FingerprintStr
    lines: tuple[ExtractedLine, ...] = ()
    blocks: tuple[ExtractedBlock, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    trees: tuple[ExtractedTreeNode, ...] = ()
    record_count: NonNegativeInt | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    indexed_refs: Annotated[
        tuple[PhysicalSourceRef, ...],
        Field(max_length=10_000),
    ] = Field(default=(), exclude_if=lambda value: not value)
    is_last: StrictBool = False
    manifest: ExtractedDatasetManifest | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> Self:
        self._validate_terminal_manifest()
        self._validate_local_ids()
        self._validate_locations()
        self._validate_schema_extensions()
        self._validate_generated_physical_ids()
        self._validate_indexed_refs()
        self._validate_manifest_index()
        if self.manifest is not None:
            self.manifest.validate_batch(self)
        return self

    def to_summary(self) -> ExtractedBatchSummary:
        """Вернуть serializable summary без raw содержимого batch."""

        return ExtractedBatchSummary(
            batch_index=self.batch_index,
            batch_fingerprint=self.batch_fingerprint,
            source=self.source,
            extraction_id=self.extraction_id,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            physical_ref_count=self._physical_ref_count(),
            record_count=self.record_count,
        )

    def physical_refs(self) -> tuple[PhysicalSourceRef, ...]:
        """Вернуть ссылки на все адресуемые физические объекты batch.

        Returns:
            Новый immutable tuple в детерминированном порядке обхода объектов.

        Метод не возвращает raw values и не выполняет I/O.
        """

        return self._physical_refs()

    def _validate_terminal_manifest(self) -> None:
        if self.is_last and self.manifest is None:
            raise ValueError("Terminal ExtractedBatch обязан содержать manifest")
        if not self.is_last and self.manifest is not None:
            raise ValueError("Manifest допустим только в terminal ExtractedBatch")
        if self.manifest is None:
            return
        manifest = self.manifest
        if (
            manifest.source != self.source
            or manifest.extraction_id != self.extraction_id
            or manifest.parser_id != self.parser_id
            or manifest.parser_version != self.parser_version
        ):
            raise ValueError("Manifest принадлежит другому source/parser run")
        if manifest.schema_version != self.schema_version:
            raise ValueError("Manifest и terminal batch имеют разные schema versions")
        if not manifest.batches or manifest.batches[-1].batch_index != self.batch_index:
            raise ValueError("Terminal batch должен завершать manifest sequence")
        terminal = manifest.batches[-1]
        if terminal.batch_fingerprint != self.batch_fingerprint:
            raise ValueError("Terminal batch fingerprint не совпадает с manifest")

    def _validate_schema_extensions(self) -> None:
        if self.schema_version not in _SUPPORTED_EXTRACTION_SCHEMA_VERSIONS:
            raise ValueError("Неподдерживаемая версия physical extraction schema")
        has_physical_extensions = (
            any(
                (
                    isinstance(location, LineRangeLocation)
                    and location.column_start is not None
                )
                for location in self._all_locations()
            )
            or any(line.metadata for line in self._all_lines())
            or any(block.metadata for block in self.blocks)
            or any(
                table.segment_index is not None or table.metadata
                for table in self.tables
            )
            or any(cell.metadata for table in self.tables for cell in table.cells)
            or any(
                (
                    isinstance(location, JsonPointerLocation)
                    and (
                        location.record_index is not None
                        or location.line_start is not None
                        or location.occurrence_path is not None
                    )
                )
                for location in self._all_locations()
            )
            or any(
                node.node_kind is not None
                or node.raw_name is not None
                or node.raw_lexeme is not None
                or node.metadata
                or node.tree_id is not None
                for node in self.trees
            )
            or any(
                isinstance(location, CssSelectorLocation)
                and (
                    location.node_index is not None
                    or location.line_number is not None
                    or location.column_number is not None
                )
                for location in self._all_locations()
            )
            or any(
                isinstance(location, XPathLocation)
                and location.namespace_prefix is not None
                for location in self._all_locations()
            )
        )
        if self.schema_version == "1.1.0":
            if self.record_count is None:
                raise ValueError("Schema 1.1.0 требует record_count")
            return
        if (
            self.record_count is not None
            or self.indexed_refs
            or has_physical_extensions
        ):
            raise ValueError(
                "Schema 1.0.0 не поддерживает поля physical extraction 1.1"
            )

    def _all_lines(self) -> tuple[ExtractedLine, ...]:
        return (
            *self.lines,
            *(line for block in self.blocks for line in block.lines),
        )

    def _all_locations(self) -> tuple[SourceLocation, ...]:
        locations: list[SourceLocation] = []
        for line in self._all_lines():
            locations.append(line.location)
            locations.extend(value.location for value in line.values)
        for block in self.blocks:
            locations.append(block.location)
            locations.extend(value.location for value in block.values)
        for table in self.tables:
            locations.append(table.location)
            locations.extend(cell.value.location for cell in table.cells)
        for node in self.trees:
            locations.append(node.location)
            if node.value is not None:
                locations.append(node.value.location)
        return tuple(locations)

    def _validate_indexed_refs(self) -> None:
        if len(self.indexed_refs) != len(set(self.indexed_refs)):
            raise ValueError("Indexed physical refs должны быть уникальны")
        if not self._contains_physical_refs(self.indexed_refs):
            raise ValueError("Indexed refs содержат отсутствующий physical ID")

    def _validate_generated_physical_ids(self) -> None:
        keys = self._physical_ref_keys()
        first = next(keys, None)
        if first is None:
            return
        _validate_generated_extraction_id(self.extraction_id)
        _validate_generated_physical_local_id(*first)
        for kind, local_id in keys:
            _validate_generated_physical_local_id(kind, local_id)

    def _validate_local_ids(self) -> None:
        all_lines = (
            *self.lines,
            *(line for block in self.blocks for line in block.lines),
        )
        all_cells = tuple(cell for table in self.tables for cell in table.cells)
        all_values = (
            *(value for line in all_lines for value in line.values),
            *(value for block in self.blocks for value in block.values),
            *(cell.value for cell in all_cells),
            *(node.value for node in self.trees if node.value is not None),
        )
        collections = (
            tuple(line.line_id for line in all_lines),
            tuple(block.block_id for block in self.blocks),
            tuple(table.table_id for table in self.tables),
            tuple(node.node_id for node in self.trees),
            tuple(cell.cell_id for cell in all_cells),
            tuple(value.value_id for value in all_values if value.value_id is not None),
        )
        if any(len(ids) != len(set(ids)) for ids in collections):
            raise ValueError("Physical IDs должны быть уникальны внутри kind/batch")
        self._validate_tree_links()

    def _validate_tree_links(self) -> None:
        parents = {node.node_id: node.parent_id for node in self.trees}
        if any(
            parent is not None and parent not in parents for parent in parents.values()
        ):
            raise ValueError("Tree node ссылается на отсутствующий parent")
        resolved: set[str] = set()
        for node_id in parents:
            visited: set[str] = set()
            current: str | None = node_id
            while current is not None and current not in resolved:
                if current in visited:
                    raise ValueError("Physical tree не может содержать cycle")
                visited.add(current)
                current = parents[current]
            resolved.update(visited)
        children_by_parent: dict[str, list[int]] = {}
        for node in self.trees:
            if node.parent_id is None:
                continue
            children_by_parent.setdefault(node.parent_id, []).append(node.order)
        for node in self.trees:
            if node.tree_id is None:
                continue
            child_start = node.child_start_index
            child_count = node.child_count
            if child_start is None or child_count is None:
                raise ValueError("Некорректный tree segment contract")
            child_orders = sorted(children_by_parent.get(node.node_id, ()))
            if len(child_orders) != child_count or any(
                order != child_start + offset
                for offset, order in enumerate(child_orders)
            ):
                raise ValueError(
                    "Tree segment child range не совпадает с direct children"
                )

    def _validate_locations(self) -> None:
        if any(location.source != self.source for location in self._all_locations()):
            raise ValueError("Physical location принадлежит другому source snapshot")

    def _validate_manifest_index(self) -> None:
        if self.manifest is None:
            return
        indexed = {
            reference
            for reference in self.manifest.source_index.refs
            if reference.batch_index == self.batch_index
        }
        if not self._contains_physical_refs(indexed):
            raise ValueError(
                "Source index содержит отсутствующие physical IDs terminal batch"
            )

    def _physical_ref_keys(self) -> Iterator[_PhysicalRefKey]:
        """Лениво перечислить physical keys без создания provenance DTO."""

        for line in self.lines:
            yield PhysicalObjectKind.LINE, line.line_id
        for block in self.blocks:
            for line in block.lines:
                yield PhysicalObjectKind.LINE, line.line_id
        for block in self.blocks:
            yield PhysicalObjectKind.BLOCK, block.block_id
        for table in self.tables:
            yield PhysicalObjectKind.TABLE, table.table_id
        for table in self.tables:
            for cell in table.cells:
                yield PhysicalObjectKind.CELL, cell.cell_id
        for node in self.trees:
            yield PhysicalObjectKind.TREE_NODE, node.node_id

        for line in self.lines:
            for value in line.values:
                if value.value_id is not None:
                    yield PhysicalObjectKind.VALUE, value.value_id
        for block in self.blocks:
            for line in block.lines:
                for value in line.values:
                    if value.value_id is not None:
                        yield PhysicalObjectKind.VALUE, value.value_id
        for block in self.blocks:
            for value in block.values:
                if value.value_id is not None:
                    yield PhysicalObjectKind.VALUE, value.value_id
        for table in self.tables:
            for cell in table.cells:
                if cell.value.value_id is not None:
                    yield PhysicalObjectKind.VALUE, cell.value.value_id
        for node in self.trees:
            if node.value is not None and node.value.value_id is not None:
                yield PhysicalObjectKind.VALUE, node.value.value_id

    def _physical_ref_count(self) -> int:
        """Посчитать адресуемые объекты без materialization provenance DTO."""

        line_count = len(self.lines) + sum(len(block.lines) for block in self.blocks)
        cell_count = sum(len(table.cells) for table in self.tables)
        value_count = (
            sum(
                value.value_id is not None
                for line in self.lines
                for value in line.values
            )
            + sum(
                value.value_id is not None
                for block in self.blocks
                for line in block.lines
                for value in line.values
            )
            + sum(
                value.value_id is not None
                for block in self.blocks
                for value in block.values
            )
            + sum(
                cell.value.value_id is not None
                for table in self.tables
                for cell in table.cells
            )
            + sum(
                node.value is not None and node.value.value_id is not None
                for node in self.trees
            )
        )
        return (
            line_count
            + len(self.blocks)
            + len(self.tables)
            + cell_count
            + len(self.trees)
            + value_count
        )

    def _contains_physical_refs(
        self,
        references: Iterable[PhysicalSourceRef],
    ) -> bool:
        """Проверить bounded refs по ленивым keys текущего batch."""

        missing: set[_PhysicalRefKey] = set()
        for reference in references:
            if (
                reference.extraction_id != self.extraction_id
                or reference.batch_index != self.batch_index
            ):
                return False
            missing.add((reference.kind, reference.local_id))
        if not missing:
            return True
        for key in self._physical_ref_keys():
            missing.discard(key)
            if not missing:
                return True
        return False

    def _physical_refs(self) -> tuple[PhysicalSourceRef, ...]:
        return tuple(
            PhysicalSourceRef(
                extraction_id=self.extraction_id,
                batch_index=self.batch_index,
                kind=kind,
                local_id=local_id,
            )
            for kind, local_id in self._physical_ref_keys()
        )
