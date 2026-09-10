"""Локальные группы блоков и sections; геометрия не превращается в OCR."""

from decimal import Decimal

from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.source import ExtractedBlockKind
from structuraguard.contracts.structure import DocumentObservation
from structuraguard.structure._observations import Observations
from structuraguard.structure._samples import Block


def _pair(text: str) -> tuple[str, str] | None:
    for delimiter in (":", "="):
        key, separator, value = text.partition(delimiter)
        if separator and 0 < len(key.strip()) <= 128 and value.strip():
            return key.strip(), value.strip()
    return None


def analyze_documents(output: Observations) -> None:
    blocks = output.samples.blocks
    if not blocks:
        return
    groups: dict[tuple[str, ...], list[tuple[Block, ...]]] = {}
    current: list[Block] = []

    def section() -> None:
        if not current:
            return
        first, last = current[0], current[-1]
        if last.order < first.order:
            return
        if len(current) > 64:
            output.samples.reasons.add("block_group_limit")
            return
        signature = tuple(block.kind.value for block in current[:64])
        output.add(
            DocumentObservation(
                source_refs=tuple(dict.fromkeys((first.reference, last.reference))),
                role="section",
                block_start=first.order,
                block_end=last.order,
                label=first.text if first.kind is ExtractedBlockKind.HEADING else None,
                block_kinds=signature,
            ),
            Decimal("0.75"),
            candidate=ParsePlanKind.DOCUMENT,
        )
        if signature not in groups and len(groups) >= output.options.max_patterns:
            output.samples.reasons.add("pattern_limit")
        else:
            groups.setdefault(signature, []).append(tuple(current))

    for offset, block in enumerate(blocks):
        if block.kind is ExtractedBlockKind.HEADING:
            section()
            current = []
            output.add(
                DocumentObservation(
                    source_refs=(block.reference,),
                    role="heading",
                    block_start=block.order,
                    block_end=block.order,
                    label=block.text,
                ),
                Decimal("0.95"),
            )
        current.append(block)
        pair = _pair(block.text)
        if pair:
            output.add(
                DocumentObservation(
                    source_refs=(block.reference,),
                    role="key_value",
                    block_start=block.order,
                    block_end=block.order,
                    label=pair[0],
                ),
                Decimal("0.75"),
                candidate=ParsePlanKind.DOCUMENT,
            )
        elif block.text.rstrip().endswith((":", "=")) and offset + 1 < len(blocks):
            nearby = blocks[offset + 1]
            if (
                nearby.order == block.order + 1
                and nearby.kind is not ExtractedBlockKind.HEADING
            ):
                output.add(
                    DocumentObservation(
                        source_refs=(block.reference, nearby.reference),
                        role="key_value",
                        block_start=block.order,
                        block_end=nearby.order,
                        label=block.text.rstrip()[:-1],
                    ),
                    Decimal("0.6"),
                    candidate=ParsePlanKind.DOCUMENT,
                )
    section()
    # Короткие повторяющиеся группы возможны и без headings. Это слабая
    # альтернативная гипотеза, а не предписание объединить все paragraphs.
    for offset in range(0, len(blocks) - 1, 2):
        pair_blocks = (blocks[offset], blocks[offset + 1])
        if pair_blocks[1].order < pair_blocks[0].order:
            continue
        signature = tuple(block.kind.value for block in pair_blocks)
        if signature not in groups and len(groups) >= output.options.max_patterns:
            output.samples.reasons.add("pattern_limit")
            break
        groups.setdefault(signature, []).append(pair_blocks)
    for signature, repeated in groups.items():
        repeated = list(
            {
                tuple(block.reference for block in group): group for group in repeated
            }.values()
        )
        if len(repeated) < 2:
            continue
        first, last = repeated[0][0], repeated[-1][-1]
        if last.order < first.order:
            continue
        output.add(
            DocumentObservation(
                source_refs=tuple(group[0].reference for group in repeated[:4]),
                role="repeated_group",
                block_start=first.order,
                block_end=last.order,
                occurrences=len(repeated),
                block_kinds=signature,
            ),
            Decimal("0.75"),
            candidate=ParsePlanKind.DOCUMENT,
        )
    for table_id, table in output.samples.tables.items():
        output.add(
            DocumentObservation(
                source_refs=(table.reference,),
                role="table",
                table_id=table_id,
            ),
            Decimal("1"),
            candidate=ParsePlanKind.DOCUMENT,
        )
