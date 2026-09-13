"""Bounded source/normalized DTO и runtime lease без открытия path/URL."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self

from structuraguard.contracts.common import NullScalar, PipelineStatus, StringScalar
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedDatasetManifest,
)
from structuraguard.contracts.parsing import ValidatedParsePlan
from structuraguard.contracts.privacy import PrivacySummary
from structuraguard.contracts.reports import SemanticParseReport
from structuraguard.contracts.source import (
    ExtensionMetadataEntry,
    ExtractedBatch,
    ExtractedDatasetManifest,
    ExtractedLine,
    ProbeResult,
    SourceArtifact,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.resources import SourceStream
from structuraguard.security.classification import ContentProtector
from structuraguard.security.patterns import field_category

from .session import RunSession


@dataclass(frozen=True, kw_only=True)
class SourceRequest:
    """Параметры чтения источника через принадлежащий приложению SourceStream.

    ``stream`` предоставляет async read(size); ``display_name`` и ``media_type``
    служат недоверенными подсказками detection, а ``expected_size`` — заявленным
    размером для resource gate. Metadata не открывает path/URL и не заменяет
    проверку фактически прочитанных bytes. Создание DTO не выполняет I/O;
    limits и отказы проверяются при inspect_source/ingest. Stream закрывает host.
    """

    stream: SourceStream = field(repr=False)
    display_name: str = "source"
    media_type: str = "application/octet-stream"
    expected_size: int | None = None


@dataclass(frozen=True, kw_only=True)
class SourceAnalysis:
    """Физический snapshot и lease, полученные через SDK.inspect_source.

    ``artifact``/``probe`` описывают источник и detection; ``batches``/``manifest``
    содержат physical data и evidence. DTO создаёт SDK: копирование или подмена
    полей не даёт полномочий на выполнение в другом run. Данные чувствительны.
    ``async with source`` закрывает ресурсы обработки, сохраняя host transport.
    """

    artifact: SourceArtifact
    probe: ProbeResult
    batches: tuple[ExtractedBatch, ...] = field(repr=False)
    manifest: ExtractedDatasetManifest = field(repr=False)
    _run: RunSession = field(repr=False, compare=False)

    @property
    def status(self) -> PipelineStatus:
        """Вернуть текущий статус run без I/O; это не разрешение на новый stage."""
        return self._run.result.status

    async def replay(self) -> AsyncIterator[ExtractedBatch]:
        """Повторно выдать physical batches из памяти без чтения transport.

        Закрытый lease даёт StructuraGuardError, исчерпанный deadline —
        SecurityPolicyError. Итерация не заменяет ParsePlan validation.
        """
        self._run.ensure_open()
        for batch in self.batches:
            self._run.resources.check_deadline()
            yield batch

    async def aclose(self) -> None:
        """Закрыть ресурсы обработки с конечным cleanup budget; повтор допустим.

        Host transport/clients не закрываются. Возможен cleanup staging через
        adapter; его сбой сохраняется в warnings run. CancelledError до commit
        распространяется, подтверждённый commit сохраняется как outcome.
        """
        await self._run.aclose()

    async def __aenter__(self) -> Self:
        self._run.ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


@dataclass(frozen=True, kw_only=True)
class NormalizedData:
    """Завершённый semantic snapshot, созданный SDK.parse_semantically.

    ``source`` связывает lease и physical evidence; ``batches``/``manifest``
    содержат normalized records; ``report`` и validated ``plan`` подтверждают
    выполненный разбор. Поля могут содержать PII. DTO не разрешает DB load:
    перед ним SDK проверяет mapping/records, scope и security policy.
    """

    source: SourceAnalysis = field(repr=False)
    batches: tuple[NormalizedBatch, ...] = field(repr=False)
    manifest: NormalizedDatasetManifest = field(repr=False)
    report: SemanticParseReport = field(repr=False)
    plan: ValidatedParsePlan = field(repr=False)

    async def replay(self) -> AsyncIterator[NormalizedBatch]:
        """Повторно выдать batches из памяти без I/O и новой валидации.

        Закрытый source lease даёт StructuraGuardError. Deadline контролируется
        потребляющим stage; сам iterator не даёт разрешения на загрузку.
        """
        self.source._run.ensure_open()
        for batch in self.batches:
            yield batch


def metadata_text(entries: tuple[ExtensionMetadataEntry, ...]) -> tuple[str, ...]:
    return tuple(
        text
        for entry in entries
        for text in (entry.key, entry.value)
        if isinstance(text, str)
    )


def line_text(line: ExtractedLine) -> tuple[str, ...]:
    return (
        line.text,
        *(
            value.raw_value.value
            for value in line.values
            if isinstance(value.raw_value, StringScalar)
        ),
        *metadata_text(line.metadata),
    )


async def _text_parts(source: SourceAnalysis) -> AsyncIterator[str]:
    """Сканировать raw текст, вложенные строки, значения и parser metadata."""
    yield source.artifact.display_name
    for batch in source.batches:
        for line in batch.lines:
            for text in line_text(line):
                yield text
        for block in batch.blocks:
            if block.text:
                yield block.text
            for value in block.values:
                if isinstance(value.raw_value, StringScalar):
                    yield value.raw_value.value
            for line in block.lines:
                for text in line_text(line):
                    yield text
            for text in metadata_text(block.metadata):
                yield text
        for table in batch.tables:
            for cell in table.cells:
                if isinstance(cell.value.raw_value, StringScalar):
                    yield cell.value.raw_value.value
                for text in metadata_text(cell.metadata):
                    yield text
            for text in metadata_text(table.metadata):
                yield text
        for tree in batch.trees:
            yield tree.raw_name if tree.raw_name is not None else tree.name
            if tree.value and isinstance(tree.value.raw_value, StringScalar):
                yield tree.value.raw_value.value
            if tree.raw_lexeme:
                yield tree.raw_lexeme
            for text in metadata_text(tree.metadata):
                yield text


async def text_chunks(source: SourceAnalysis) -> AsyncIterator[str]:
    """Разделить физические поля: соседний label не должен скрывать regex boundary."""
    async for text in _text_parts(source):
        yield text + "\n"


async def classify_source(source: SourceAnalysis) -> tuple[PrivacySummary, ...]:
    """Применить M14 text и credential-field detection к одному bounded snapshot.

    Имена JSON/XML полей и табличных колонок — дополнительные privacy hints,
    которые только повышают classification. Они не задают semantic ParsePlan.
    Credential hint родителя сохраняется для XML text nodes и nested values.
    """
    run = source._run
    protector = ContentProtector(run.dependencies.privacy, resources=run.resources)
    text_report = await protector.classify_chunks(text_chunks(source))
    custom = tuple(
        "".join(char for char in name.casefold() if char.isalnum())
        for name in protector.policy.custom_secret_fields
    )
    fields: dict[str, list[str]] = {}
    tree_hints: dict[str, str] = {}
    column_hints: dict[tuple[str, int], str] = {}
    first_rows: dict[str, int] = {}
    chars = 0

    def add(name: str, value: str) -> None:
        nonlocal chars
        if not value or field_category(name, custom) is None:
            return
        chars += len(value) + 1 + (len(name) if name not in fields else 0)
        if chars > protector.policy.limits.max_chars or (
            name not in fields and len(fields) >= protector.policy.limits.max_fields
        ):
            raise SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED", message="SECURITY_LIMIT_EXCEEDED"
            )
        fields.setdefault(name, []).append(value)

    def metadata(entries: tuple[ExtensionMetadataEntry, ...]) -> None:
        for entry in entries:
            if entry.value is not None:
                add(entry.key, str(entry.value))

    for batch in source.batches:
        run.resources.check_deadline()
        for line in (
            *batch.lines,
            *(line for block in batch.blocks for line in block.lines),
        ):
            metadata(line.metadata)
        for block in batch.blocks:
            metadata(block.metadata)
        for table in batch.tables:
            metadata(table.metadata)
            if table.cells:
                first_rows.setdefault(
                    table.table_id, min(cell.row_index for cell in table.cells)
                )
            for cell in table.cells:
                value = cell.value.raw_value
                if (
                    cell.row_index == first_rows[table.table_id]
                    and isinstance(value, StringScalar)
                    and field_category(value.value, custom) is not None
                ):
                    column_hints[(table.table_id, cell.column_index)] = value.value
            for cell in table.cells:
                metadata(cell.metadata)
                hint = column_hints.get((table.table_id, cell.column_index))
                if (
                    hint
                    and cell.row_index != first_rows[table.table_id]
                    and not isinstance(cell.value.raw_value, NullScalar)
                ):
                    add(hint, cell.value.raw_value.canonical_json())
        for node in batch.trees:
            metadata(node.metadata)
            name = node.raw_name if node.raw_name is not None else node.name
            hint = tree_hints.get(node.parent_id or "")
            if hint is None and field_category(name, custom) is not None:
                hint = name
            if hint:
                tree_hints[node.node_id] = hint
                if node.value and not isinstance(node.value.raw_value, NullScalar):
                    add(hint, node.value.raw_value.canonical_json())
    if not fields:
        return (text_report.safe_summary(),)
    named = await protector.classify_fields(
        {name: "\n".join(values) for name, values in fields.items()},
        minimum_classification=text_report.classification,
    )
    return (text_report.safe_summary(), named.safe_summary())
