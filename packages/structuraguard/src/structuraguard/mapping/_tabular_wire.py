"""Отдельное решение по каждому источнику; публичный план остаётся прежним."""

import copy
import re
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StrictStr, model_validator

from structuraguard.contracts.deterministic_mapping import SensitiveMappingContract
from structuraguard.contracts.tabular_import import (
    TabularCopyChoice,
    TabularImportChoice,
    TabularImportOmission,
    TabularImportSuggestion,
    TabularLiteralSplitChoice,
    TabularSourceId,
    TabularTargetId,
    TabularWhitespaceSplitChoice,
    TabularWireScore,
)
from structuraguard.llm._structured import parse_object


class _TabularFieldDecision(SensitiveMappingContract):
    source_id: TabularSourceId
    explanation: Annotated[StrictStr, Field(min_length=1, max_length=120)]
    operation: Literal["copy", "split", "omit"]
    target_ids: Annotated[tuple[TabularTargetId, ...], Field(max_length=8)]
    split_mode: Literal["whitespace", "literal"] | None
    delimiter: Annotated[StrictStr, Field(min_length=1, max_length=8)] | None
    confidence: TabularWireScore

    @model_validator(mode="after")
    def _operation(self) -> Self:
        if len(set(self.target_ids)) != len(self.target_ids):
            raise ValueError("Цели одного исходного поля должны быть уникальны")
        if self.operation in {"copy", "omit"}:
            if (
                len(self.target_ids) != (1 if self.operation == "copy" else 0)
                or self.split_mode is not None
                or self.delimiter is not None
            ):
                raise ValueError(
                    "copy/omit требуют правильное число целей и null параметры"
                )
        elif (
            len(self.target_ids) < 2
            or self.split_mode is None
            or (self.split_mode == "whitespace" and self.delimiter is not None)
            or (self.split_mode == "literal" and self.delimiter is None)
        ):
            raise ValueError("split требует все цели частей и согласованные параметры")
        return self


class TabularImportWireResponse(SensitiveMappingContract):
    """Короткое объяснение предшествует решению и не переносится в публичный план."""

    fields: Annotated[
        tuple[_TabularFieldDecision, ...], Field(min_length=1, max_length=128)
    ]
    confidence: TabularWireScore
    decision: Literal["map", "ambiguous", "unsupported"]

    @model_validator(mode="after")
    def _sources(self) -> Self:
        if len({field.source_id for field in self.fields}) != len(self.fields):
            raise ValueError("Каждый источник должен иметь одно решение")
        return self


def as_public_suggestion(wire: TabularImportWireResponse) -> TabularImportSuggestion:
    """Развернуть явное решение модели без выбора смысла, repair или потери частей."""
    if wire.decision != "map":
        return TabularImportSuggestion(
            decision=wire.decision,
            assignments=(),
            omissions=(),
            confidence=wire.confidence,
            reason=wire.decision,
        )
    assignments: list[TabularImportChoice] = []
    omissions: list[TabularImportOmission] = []
    for field in wire.fields:
        if field.operation == "omit":
            omissions.append(
                TabularImportOmission(
                    source_id=field.source_id,
                    reason="no_target_column",
                    confidence=field.confidence,
                )
            )
        elif field.operation == "copy":
            assignments.append(
                TabularCopyChoice(
                    source_id=field.source_id,
                    target_id=field.target_ids[0],
                    operation="copy",
                    split_mode=None,
                    delimiter=None,
                    part_count=None,
                    part_index=None,
                    confidence=field.confidence,
                    reason="semantic_equivalence",
                )
            )
        else:
            for index, target in enumerate(field.target_ids):
                if field.split_mode == "whitespace":
                    item: TabularImportChoice = TabularWhitespaceSplitChoice(
                        source_id=field.source_id,
                        target_id=target,
                        operation="split",
                        split_mode="whitespace",
                        delimiter=None,
                        part_count=len(field.target_ids),
                        part_index=index,
                        confidence=field.confidence,
                        reason="composite_component",
                    )
                else:
                    assert field.delimiter is not None
                    item = TabularLiteralSplitChoice(
                        source_id=field.source_id,
                        target_id=target,
                        operation="split",
                        split_mode="literal",
                        delimiter=field.delimiter,
                        part_count=len(field.target_ids),
                        part_index=index,
                        confidence=field.confidence,
                        reason="composite_component",
                    )
                assignments.append(item)
    return TabularImportSuggestion(
        decision="map",
        assignments=tuple(assignments),
        omissions=tuple(omissions),
        confidence=wire.confidence,
        reason="semantic_equivalence",
    )


def _aliases(items: object, key: str, prefix: str, maximum: int) -> list[str] | None:
    if not isinstance(items, list) or not 1 <= len(items) <= maximum:
        return None
    aliases: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        alias = item.get(key)
        if (
            not isinstance(alias, str)
            or re.fullmatch(prefix + r"[0-9]{1,3}", alias) is None
        ):
            return None
        aliases.append(alias)
    return aliases if len(set(aliases)) == len(aliases) else None


def project_tabular_import_schema(
    base: dict[str, object], payload_json: str
) -> dict[str, object]:
    """Фиксировать только покрытие aliases; смысл, цель и действие выбирает LLM.

    До 16 источников получают отдельные позиции grammar. Более широкие таблицы
    используют общую схему и обычную обязательную проверку покрытия в binder.
    Исходные значения и названия никогда не вставляются в native schema.
    """
    payload = parse_object(payload_json)
    sources = _aliases(payload.get("source_fields"), "source_id", "s", 16)
    targets = _aliases(payload.get("target_columns"), "target_id", "c", 128)
    if sources is None or targets is None:
        return base
    definitions = cast(dict[str, dict[str, object]], base["$defs"])
    template = definitions["_TabularFieldDecision"]
    fields: list[dict[str, object]] = []
    for alias in sources:
        field = copy.deepcopy(template)
        properties = cast(dict[str, object], field["properties"])
        properties["source_id"] = {"type": "string", "const": alias}
        properties["target_ids"] = {
            "type": "array",
            "items": {"type": "string", "enum": targets},
            "maxItems": min(8, len(targets)),
        }
        fields.append(field)
    projected = copy.deepcopy(base)
    properties = cast(dict[str, object], projected["properties"])
    properties["fields"] = {
        "type": "array",
        "prefixItems": fields,
        "minItems": len(sources),
        "maxItems": len(sources),
    }
    return projected
