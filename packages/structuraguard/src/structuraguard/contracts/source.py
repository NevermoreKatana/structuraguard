"""Физическая модель источника после технического parsing."""

from __future__ import annotations

import math
from collections.abc import Iterable
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
    PhysicalObjectKind,
    PhysicalSourceRef,
    PositiveInt,
    RawScalar,
    SchemaVersionStr,
    SourceArtifactRef,
    VersionStr,
    _redact_union_validation_input,
)


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


class ProbeResult(FrozenContract):
    """Результат безопасного технического определения формата."""

    source: SourceArtifactRef
    adapter_id: IdentifierStr
    adapter_version: VersionStr
    supported: StrictBool
    confidence: ConfidenceDecimal
    detected_media_type: IdentifierStr | None = None
    detected_encoding: IdentifierStr | None = None
    warnings: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def _require_detection_for_supported_source(self) -> Self:
        if self.supported and self.detected_media_type is None:
            raise ValueError("Поддерживаемый источник должен иметь detected_media_type")
        if len(self.warnings) != len(set(self.warnings)):
            raise ValueError("Probe warnings должны быть уникальны")
        return self


class LineRangeLocation(FrozenContract):
    """Диапазон строк исходного snapshot, нумерация начинается с единицы."""

    kind: Literal["line_range"] = "line_range"
    source: SourceArtifactRef
    line_start: PositiveInt
    line_end: PositiveInt

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end не может предшествовать line_start")
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
    sheet_name: IdentifierStr
    row_index: NonNegativeInt
    column_index: NonNegativeInt


class JsonPointerLocation(FrozenContract):
    """RFC 6901 JSON Pointer внутри snapshot."""

    kind: Literal["json_pointer"] = "json_pointer"
    source: SourceArtifactRef
    pointer: Annotated[StrictStr, StringConstraints(max_length=4096)]

    @model_validator(mode="after")
    def _validate_pointer(self) -> Self:
        if self.pointer and not self.pointer.startswith("/"):
            raise ValueError("JSON Pointer должен быть пустым или начинаться с '/'")
        return self


class XPathLocation(FrozenContract):
    """Ограниченная XPath-ссылка; исполнение выражения остаётся в parser adapter."""

    kind: Literal["xpath"] = "xpath"
    source: SourceArtifactRef
    xpath: Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096)]


class CssSelectorLocation(FrozenContract):
    """CSS-селектор физического элемента source snapshot."""

    kind: Literal["css_selector"] = "css_selector"
    source: SourceArtifactRef
    selector: Annotated[StrictStr, StringConstraints(min_length=1, max_length=4096)]


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

    @model_validator(mode="after")
    def _validate_line_location(self) -> Self:
        if isinstance(self.location, LineRangeLocation) and not (
            self.location.line_start <= self.line_number <= self.location.line_end
        ):
            raise ValueError("line_number не входит в physical line range")
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

    @model_validator(mode="after")
    def _require_physical_content(self) -> Self:
        if self.text is None and not self.values and not self.lines:
            raise ValueError("ExtractedBlock требует raw text, values или lines")
        if (
            isinstance(self.location, DocumentBlockLocation)
            and self.location.block_id != self.block_id
        ):
            raise ValueError("block_id не совпадает с document block location")
        return self


class ExtractedCell(FrozenContract):
    """Raw ячейка физической таблицы."""

    cell_id: IdentifierStr
    row_index: NonNegativeInt
    column_index: NonNegativeInt
    value: ExtractedValue

    @model_validator(mode="after")
    def _validate_cell_location(self) -> Self:
        location = self.value.location
        if isinstance(location, TabularCellLocation | SheetCellLocation) and (
            location.row_index != self.row_index
            or location.column_index != self.column_index
        ):
            raise ValueError("cell coordinates не совпадают с physical location")
        return self


class ExtractedTable(FrozenContract):
    """Физическая таблица с raw cells и без semantic field names."""

    table_id: IdentifierStr
    location: SourceLocation
    cells: tuple[ExtractedCell, ...] = ()

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
        return self


class ExtractedTreeNode(FrozenContract):
    """Физический узел дерева без окончательной semantic интерпретации."""

    node_id: IdentifierStr
    parent_id: IdentifierStr | None = None
    name: IdentifierStr
    order: NonNegativeInt
    location: SourceLocation
    value: ExtractedValue | None = None

    @model_validator(mode="after")
    def _reject_self_parent(self) -> Self:
        if self.parent_id == self.node_id:
            raise ValueError("Tree node не может ссылаться на себя как parent")
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


class ExtractedDatasetManifest(FrozenContract):
    """Aggregate identity полного непрерывного technical extraction run."""

    schema_version: SchemaVersionStr = "1.0.0"
    source: SourceArtifactRef
    extraction_id: IdentifierStr
    parser_id: IdentifierStr
    parser_version: VersionStr
    batches: tuple[ExtractedBatchSummary, ...]
    extraction_fingerprint: FingerprintStr
    source_index: ExtractedSourceIndex

    @model_validator(mode="after")
    def _validate_run(self) -> Self:
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
        return self

    def validate_batch(self, batch: ExtractedBatch) -> None:
        """Сверить один полученный batch с manifest и bounded source index."""

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
        actual_refs = set(batch._physical_refs())
        indexed_refs = {
            reference
            for reference in self.source_index.refs
            if reference.batch_index == batch.batch_index
        }
        if not indexed_refs <= actual_refs:
            raise ValueError("Source index содержит отсутствующий physical ID")

    def validate_batches(self, batches: Iterable[ExtractedBatch]) -> None:
        """Проверить полную ordered sequence без повторной загрузки raw source."""

        batch_count = 0
        for expected_index, batch in enumerate(batches):
            if expected_index >= len(self.batches):
                raise ValueError("Количество batches не совпадает с manifest")
            if batch.batch_index != expected_index:
                raise ValueError("Порядок batches не совпадает с manifest")
            self.validate_batch(batch)
            batch_count += 1
        if batch_count != len(self.batches):
            raise ValueError("Количество batches не совпадает с manifest")


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
    is_last: StrictBool = False
    manifest: ExtractedDatasetManifest | None = None

    @model_validator(mode="after")
    def _validate_batch(self) -> Self:
        self._validate_terminal_manifest()
        self._validate_local_ids()
        self._validate_locations()
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
            physical_ref_count=len(self._physical_refs()),
        )

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
        if not manifest.batches or manifest.batches[-1].batch_index != self.batch_index:
            raise ValueError("Terminal batch должен завершать manifest sequence")
        terminal = manifest.batches[-1]
        if terminal.batch_fingerprint != self.batch_fingerprint:
            raise ValueError("Terminal batch fingerprint не совпадает с manifest")

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

    def _validate_locations(self) -> None:
        locations: list[SourceLocation] = []
        for line in self.lines:
            locations.append(line.location)
            locations.extend(value.location for value in line.values)
        for block in self.blocks:
            locations.append(block.location)
            locations.extend(value.location for value in block.values)
            for line in block.lines:
                locations.append(line.location)
                locations.extend(value.location for value in line.values)
        for table in self.tables:
            locations.append(table.location)
            locations.extend(cell.value.location for cell in table.cells)
        for node in self.trees:
            locations.append(node.location)
            if node.value is not None:
                locations.append(node.value.location)
        if any(location.source != self.source for location in locations):
            raise ValueError("Physical location принадлежит другому source snapshot")

    def _validate_manifest_index(self) -> None:
        if self.manifest is None:
            return
        actual = set(self._physical_refs())
        indexed = {
            reference
            for reference in self.manifest.source_index.refs
            if reference.batch_index == self.batch_index
        }
        if not indexed <= actual:
            raise ValueError(
                "Source index содержит отсутствующие physical IDs terminal batch"
            )

    def _physical_refs(self) -> tuple[PhysicalSourceRef, ...]:
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

        def reference(kind: PhysicalObjectKind, local_id: str) -> PhysicalSourceRef:
            return PhysicalSourceRef(
                extraction_id=self.extraction_id,
                batch_index=self.batch_index,
                kind=kind,
                local_id=local_id,
            )

        return (
            *(reference(PhysicalObjectKind.LINE, line.line_id) for line in all_lines),
            *(
                reference(PhysicalObjectKind.BLOCK, block.block_id)
                for block in self.blocks
            ),
            *(
                reference(PhysicalObjectKind.TABLE, table.table_id)
                for table in self.tables
            ),
            *(reference(PhysicalObjectKind.CELL, cell.cell_id) for cell in all_cells),
            *(
                reference(PhysicalObjectKind.TREE_NODE, node.node_id)
                for node in self.trees
            ),
            *(
                reference(PhysicalObjectKind.VALUE, value.value_id)
                for value in all_values
                if value.value_id is not None
            ),
        )
