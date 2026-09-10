"""Закрытые source spans и document entity proposals без сгенерированных values."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PhysicalSourceRef,
    PositiveInt,
)

Name = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Alias = Annotated[StrictStr, Field(pattern=r"^r[0-9]{1,4}$")]


class SourceTextSpan(FrozenContract):
    """Непустой диапазон [start,end) до 4096 Unicode codepoints в physical text.

    source_ref и text_fingerprint должны совпасть с immutable replay. DTO проверяет
    форму/диапазон; принадлежность source и hash проверяют validator и executor.
    Offset не является byte position или выражением для исполнения.
    """

    source_ref: PhysicalSourceRef
    start: NonNegativeInt
    end: PositiveInt
    text_fingerprint: FingerprintStr

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.end <= self.start or self.end - self.start > 4096:
            raise ValueError("Span должен быть непустым")
        return self


class DocumentSpanSelector(FrozenContract):
    """Выбрать точные source spans и соединить их одним пробелом."""

    kind: Literal["document_spans"] = "document_spans"
    spans: Annotated[tuple[SourceTextSpan, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.spans)) != len(self.spans):
            raise ValueError("Duplicate spans запрещены")
        return self


class DocumentSpanGrouping(FrozenContract):
    """Объединить parent/child entity definitions в одну логическую запись."""

    kind: Literal["document_spans"] = "document_spans"
    record_id: IdentifierStr
    anchor: SourceTextSpan


class QuotedSpan(FrozenContract):
    """Ссылка только на fragment текущего chunk; quote обязан совпасть побайтно."""

    ref: Alias
    start: NonNegativeInt
    end: PositiveInt
    quote: Annotated[StrictStr, Field(min_length=1, max_length=4096)]


class DocumentFieldProposal(FrozenContract):
    """Поле задаёт ссылки на текст, а не новое значение."""

    name: Name
    semantic_type: Literal[
        "string",
        "integer",
        "number",
        "decimal",
        "boolean",
        "date",
        "datetime",
        "unresolved",
    ]
    spans: Annotated[tuple[QuotedSpan, ...], Field(min_length=1, max_length=8)]


class DocumentEntityProposal(FrozenContract):
    """Anchor идентифицирует occurrence; одинаковый текст в других местах различен."""

    entity_id: Name
    entity_type: Name
    anchor: QuotedSpan
    parent_entity_id: Name | None
    fields: Annotated[
        tuple[DocumentFieldProposal, ...], Field(min_length=1, max_length=16)
    ]


class DocumentEntitySuggestion(FrozenContract):
    """Strict structured response одного bounded document chunk."""

    schema_version: Literal["1.0.0"]
    chunk_fingerprint: FingerprintStr
    entities: Annotated[tuple[DocumentEntityProposal, ...], Field(max_length=16)]
    unresolved_refs: Annotated[tuple[Alias, ...], Field(max_length=64)]
