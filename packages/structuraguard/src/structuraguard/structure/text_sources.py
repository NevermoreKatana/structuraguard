"""Единый выбор physical text без повторного учёта block/line/tree representations."""

from collections.abc import Iterator
from dataclasses import dataclass

from structuraguard.contracts.common import (
    PhysicalObjectKind,
    PhysicalSourceRef,
    StringScalar,
)
from structuraguard.contracts.source import ExtractedBatch, SourceLocation


@dataclass(frozen=True, slots=True)
class TextSource:
    """Текст и точная physical location остаются недоверенными данными."""

    ref: PhysicalSourceRef
    text: str
    location: SourceLocation
    hint: str


def text_sources(batch: ExtractedBatch) -> Iterator[TextSource]:
    """Предпочесть document blocks, затем lines, затем scalar tree nodes."""

    def ref(kind: PhysicalObjectKind, identifier: str) -> PhysicalSourceRef:
        return PhysicalSourceRef(
            extraction_id=batch.extraction_id,
            batch_index=batch.batch_index,
            kind=kind,
            local_id=identifier,
        )

    if batch.blocks:
        for block in batch.blocks:
            if block.text is not None:
                yield TextSource(
                    ref(PhysicalObjectKind.BLOCK, block.block_id),
                    block.text,
                    block.location,
                    block.kind.value,
                )
            else:
                for line in block.lines:
                    yield TextSource(
                        ref(PhysicalObjectKind.LINE, line.line_id),
                        line.text,
                        line.location,
                        block.kind.value,
                    )
        return
    if batch.lines:
        for line in batch.lines:
            yield TextSource(
                ref(PhysicalObjectKind.LINE, line.line_id),
                line.text,
                line.location,
                "line",
            )
        return
    for node in batch.trees:
        if node.value is not None and isinstance(node.value.raw_value, StringScalar):
            location = node.value.location
            # Только logical source paths, без filename/URI или metadata.
            data = location.model_dump()
            locator = next(
                (
                    data[key]
                    for key in ("xpath", "json_pointer", "path")
                    if isinstance(data.get(key), str)
                ),
                "",
            )
            hint = str(locator or node.raw_name or node.name)[:512]
            yield TextSource(
                ref(PhysicalObjectKind.TREE_NODE, node.node_id),
                node.value.raw_value.value,
                location,
                hint,
            )
