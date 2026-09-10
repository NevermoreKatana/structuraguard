"""Закрытая wire grammar semantic proposals; provenance/score создаёт SDK."""

from decimal import Context, Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import Field, StrictFloat, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract
from structuraguard.contracts.common import (
    ConfidenceDecimal,
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    ParsePlanKind,
    PositiveInt,
    SemanticParsingMode,
)
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.llm import LLMBudget

SemanticName = Annotated[StrictStr, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
SampleAlias = Annotated[StrictStr, Field(pattern=r"^r[0-9]{1,4}$")]


class LLMStructurePolicy(FrozenContract):
    """Ограничить один semantic request и разрешённые виды ParsePlan.

    sample selection детерминирована caller; bounded values и refs проверяются
    physical replay. Неизвестный locale/type не превращается в conversion rule.
    """

    execution: ParsePlanOptions = ParsePlanOptions()
    allowed_kinds: tuple[ParsePlanKind, ...] = tuple(ParsePlanKind)
    max_sample_values: Annotated[PositiveInt, Field(le=256)] = 100
    max_catalog_refs: Annotated[PositiveInt, Field(le=512)] = 256
    max_candidates: Annotated[PositiveInt, Field(le=16)] = 8
    max_payload_bytes: Annotated[PositiveInt, Field(le=1_048_576)] = 65536
    max_response_bytes: Annotated[PositiveInt, Field(le=1_048_576)] = 65536
    max_fields: Annotated[PositiveInt, Field(le=64)] = 64
    max_entities: Annotated[PositiveInt, Field(le=16)] = 16
    confidence_threshold: ConfidenceDecimal = Decimal("0.85")
    allowed_locales: tuple[
        Annotated[StrictStr, Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")], ...
    ] = ("en", "en-US", "en-GB", "ru", "ru-RU")

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if (
            not self.allowed_kinds
            or len(set(self.allowed_kinds)) != len(self.allowed_kinds)
            or len(set(self.allowed_locales)) != len(self.allowed_locales)
        ):
            raise ValueError(
                "Policy требует уникальные kinds/locales и непустую grammar"
            )
        return self


class LLMAnalysisContext(FrozenContract):
    """Доверенные run ID, classification и routing/redaction fingerprints.

    Контекст задаётся host и не содержит credentials или handles. Fingerprints
    связывают approval с run, но не доказывают удаление PII и не разрешают egress:
    каждый minimized payload должен отдельно получить trusted scanner approval.
    """

    run_id: IdentifierStr
    data_classification: DataClassification
    routing_policy_id: IdentifierStr
    routing_policy_fingerprint: FingerprintStr
    redaction_fingerprint: FingerprintStr


class SemanticPathStep(FrozenContract):
    """Literal physical key либо array item; без JSONPath/XPath expressions."""

    operation: Literal["key", "item"]
    name: Annotated[StrictStr, Field(max_length=256)]
    occurrence: Annotated[NonNegativeInt, Field(le=10000)]

    @model_validator(mode="after")
    def _item(self) -> Self:
        if self.operation == "item" and (self.name or self.occurrence):
            raise ValueError("item не принимает key/occurrence")
        return self


class SemanticSelector(FrozenContract):
    """Закрытые selections; неприменимые поля обязаны оставаться null/empty."""

    kind: Literal["column", "tree", "log_piece", "log_record", "document"]
    index: NonNegativeInt | None
    offset: NonNegativeInt | None
    path: Annotated[tuple[SemanticPathStep, ...], Field(max_length=30)]
    value_source: Literal["node_value", "node_name"] | None
    delimiter: Literal["whitespace", "tab", "comma", "pipe", "colon", "equals"] | None
    target: Literal["block_text", "key", "value"] | None
    key_equals: Annotated[StrictStr, Field(max_length=256)] | None


class SemanticFieldProposal(FrozenContract):
    """Semantic name/type hints не изменяют raw source values."""

    field_id: SemanticName
    semantic_name: SemanticName
    semantic_type: Literal[
        "unresolved",
        "string",
        "integer",
        "number",
        "decimal",
        "boolean",
        "date",
        "datetime",
    ]
    locale_hint: Annotated[StrictStr, Field(pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")] | None
    source_refs: Annotated[tuple[SampleAlias, ...], Field(min_length=1, max_length=64)]
    selector: SemanticSelector


class SemanticEntityProposal(FrozenContract):
    """Entity groups: row, tree path либо явные конечные log/document records."""

    entity_id: SemanticName
    entity_type: SemanticName
    parent_entity_id: SemanticName | None
    field_ids: Annotated[tuple[SemanticName, ...], Field(min_length=1, max_length=64)]
    path: Annotated[tuple[SemanticPathStep, ...], Field(max_length=30)]
    records: Annotated[
        tuple[
            Annotated[tuple[SampleAlias, ...], Field(min_length=1, max_length=64)], ...
        ],
        Field(max_length=256),
    ]


class SemanticPlanProposal(FrozenContract):
    """Только semantic grammar; hashes, producer и final confidence не от модели."""

    kind: Literal["tabular", "tree", "log", "document"]
    root_ref: SampleAlias | None
    header_row: NonNegativeInt | None
    data_start_row: NonNegativeInt | None
    data_end_row: NonNegativeInt | None
    footer_start_row: NonNegativeInt | None
    repeated_header_rows: Annotated[tuple[NonNegativeInt, ...], Field(max_length=256)]
    scope: Annotated[tuple[SampleAlias, ...], Field(max_length=512)]
    fields: Annotated[
        tuple[SemanticFieldProposal, ...], Field(min_length=1, max_length=64)
    ]
    entities: Annotated[
        tuple[SemanticEntityProposal, ...], Field(min_length=1, max_length=16)
    ]


class LLMStructureSuggestion(FrozenContract):
    """Native strict schema: все поля required, nullable вместо default filling."""

    schema_version: Literal["1.0.0"]
    decision: Literal["plan", "ambiguous", "unsupported"]
    candidate_ids: Annotated[
        tuple[Annotated[StrictStr, Field(pattern=r"^c[0-9]{1,2}$")], ...],
        Field(max_length=16),
    ]
    self_confidence: Annotated[StrictFloat, Field(ge=0, le=1)]
    plan: SemanticPlanProposal | None

    @model_validator(mode="after")
    def _decision(self) -> Self:
        if (self.decision == "plan") != (self.plan is not None):
            raise ValueError("Только plan decision требует proposal")
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("Duplicate candidate aliases запрещены")
        return self


class SemanticConfidence(FrozenContract):
    """Воспроизводимый score evidence/validation/agreement с явным penalty.

    hybrid_v1 проверяет формулу с Decimal; несогласованный score даёт Pydantic
    ValidationError. Model self-confidence не входит в расчёт. Score — эвристика,
    не вероятность правильной семантики; issues требуют review при любом score.
    """

    policy_version: Literal["hybrid_v1"] = "hybrid_v1"
    deterministic_evidence: ConfidenceDecimal
    validation: ConfidenceDecimal
    agreement: ConfidenceDecimal
    penalty: ConfidenceDecimal
    confidence: ConfidenceDecimal

    @model_validator(mode="after")
    def _score(self) -> Self:
        with localcontext(Context(prec=28)):
            expected = max(
                Decimal(0),
                self.deterministic_evidence * Decimal("0.2")
                + self.validation * Decimal("0.5")
                + self.agreement * Decimal("0.3")
                - self.penalty,
            )
        if self.confidence != expected:
            raise ValueError("Confidence не соответствует hybrid_v1 evidence")
        return self


class ParsingPolicy(FrozenContract):
    """Неизменяемые limits semantic run; default mode — llm_assisted.

    ``structural`` задаёт разрешённые plans и physical replay limits, ``budget`` —
    общий calls/tokens/time предел. Chunk bytes относятся к serialized fragments;
    prompt/schema/envelope дополнительно учитываются provider. Overlap обязан быть
    меньше chunk_fragments. Остальные caps ограничивают entities, diagnostics
    и output batches; недопустимые значения дают Pydantic ValidationError.
    Policy не заменяет PII scanner/egress approval и не выполняет I/O.
    """

    mode: SemanticParsingMode = SemanticParsingMode.LLM_ASSISTED
    structural: LLMStructurePolicy = LLMStructurePolicy()
    budget: LLMBudget = LLMBudget(max_calls=16, max_tokens=524288, max_time_ms=120000)
    max_chunks: Annotated[PositiveInt, Field(le=128)] = 32
    chunk_bytes: Annotated[PositiveInt, Field(ge=256, le=16384)] = 4096
    chunk_fragments: Annotated[PositiveInt, Field(le=64)] = 16
    overlap_fragments: Annotated[NonNegativeInt, Field(le=4)] = 1
    max_document_entities: Annotated[PositiveInt, Field(le=32)] = 32
    max_issues: Annotated[PositiveInt, Field(le=1000)] = 256
    max_report_refs: Annotated[PositiveInt, Field(le=10000)] = 2048
    records_per_batch: Annotated[PositiveInt, Field(le=1000)] = 1000

    @model_validator(mode="after")
    def _overlap(self) -> Self:
        if self.overlap_fragments >= self.chunk_fragments:
            raise ValueError("Overlap должен быть меньше chunk")
        return self
