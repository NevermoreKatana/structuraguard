"""Контракты анализа структуры и декларативного плана parsing."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, WrapValidator, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.analysis import (
    AnalysisScore,
    ExplicitRecordGrouping,
    LogRecordSelector,
    PlanDerivation,
    TreeStep,
)
from structuraguard.contracts.common import (
    ConfidenceDecimal,
    FingerprintStr,
    IdentifierStr,
    IssueSeverity,
    NonNegativeInt,
    ParsePlanKind,
    PhysicalObjectKind,
    PhysicalSourceRef,
    PositiveInt,
    ProducerMetadata,
    RawScalar,
    SchemaVersionStr,
    SemanticParsingMode,
    SourceArtifactRef,
    UtcDateTime,
    ValidationDecision,
    ValidationIssue,
    VersionStr,
    _redact_union_validation_input,
)
from structuraguard.contracts.source import ExtractedDatasetManifest, SourceLocation
from structuraguard.contracts.structure import (
    DocumentObservation,
    FieldObservation,
    ProfileCoverage,
    TabularObservation,
    TextObservation,
    TreeObservation,
)

_AUTO_FINGERPRINT = "sha256:" + "0" * 64
_FINGERPRINT_FIELDS = frozenset({"fingerprint"})
_MAX_SAMPLE_VALUES = 1_000
_MAX_SAMPLE_BYTES = 1_048_576


def _reject_duplicate_refs(refs: tuple[PhysicalSourceRef, ...], *, field: str) -> None:
    if len(refs) != len(set(refs)):
        raise ValueError(f"{field} содержит повторяющиеся physical references")


class TabularShapeObservation(FrozenContract):
    """Типизированное наблюдение размеров и header-кандидатов таблицы."""

    kind: Literal["tabular_shape"] = "tabular_shape"
    table_ref: PhysicalSourceRef
    sampled_row_count: NonNegativeInt
    column_count: PositiveInt
    header_candidates: tuple[NonNegativeInt, ...] = ()

    @model_validator(mode="after")
    def _validate_shape(self) -> Self:
        if self.table_ref.kind is not PhysicalObjectKind.TABLE:
            raise ValueError("table_ref наблюдения должен ссылаться на table")
        if len(self.header_candidates) != len(set(self.header_candidates)):
            raise ValueError("header_candidates должны быть уникальны")
        if any(row >= self.sampled_row_count for row in self.header_candidates):
            raise ValueError("header candidate выходит за sampled rows")
        return self


class TreeShapeObservation(FrozenContract):
    """Типизированное наблюдение физической формы дерева."""

    kind: Literal["tree_shape"] = "tree_shape"
    root_ref: PhysicalSourceRef
    sampled_node_count: PositiveInt
    max_depth: NonNegativeInt

    @model_validator(mode="after")
    def _validate_root(self) -> Self:
        if self.root_ref.kind is not PhysicalObjectKind.TREE_NODE:
            raise ValueError("root_ref наблюдения должен ссылаться на tree node")
        return self


class LogShapeObservation(FrozenContract):
    """Типизированное наблюдение физической группы log-строк."""

    kind: Literal["log_shape"] = "log_shape"
    line_refs: tuple[PhysicalSourceRef, ...]
    boundary_candidates: tuple[Literal["one_line", "fixed_lines", "blank_line"], ...]

    @model_validator(mode="after")
    def _validate_lines(self) -> Self:
        if not self.line_refs or not self.boundary_candidates:
            raise ValueError("log observation требует строки и boundary candidates")
        _reject_duplicate_refs(self.line_refs, field="line_refs")
        if any(ref.kind is not PhysicalObjectKind.LINE for ref in self.line_refs):
            raise ValueError("line_refs наблюдения должны ссылаться на lines")
        if len(self.boundary_candidates) != len(set(self.boundary_candidates)):
            raise ValueError("boundary_candidates должны быть уникальны")
        return self


class DocumentShapeObservation(FrozenContract):
    """Типизированное наблюдение блоков и таблиц документа."""

    kind: Literal["document_shape"] = "document_shape"
    block_refs: tuple[PhysicalSourceRef, ...]
    table_refs: tuple[PhysicalSourceRef, ...] = ()

    @model_validator(mode="after")
    def _validate_document_refs(self) -> Self:
        if not self.block_refs:
            raise ValueError("document observation требует block refs")
        _reject_duplicate_refs(self.block_refs, field="block_refs")
        _reject_duplicate_refs(self.table_refs, field="table_refs")
        if any(ref.kind is not PhysicalObjectKind.BLOCK for ref in self.block_refs):
            raise ValueError("block_refs наблюдения должны ссылаться на blocks")
        if any(ref.kind is not PhysicalObjectKind.TABLE for ref in self.table_refs):
            raise ValueError("table_refs наблюдения должны ссылаться на tables")
        return self


StructureObservation = Annotated[
    TabularShapeObservation
    | TreeShapeObservation
    | LogShapeObservation
    | DocumentShapeObservation
    | TabularObservation
    | TreeObservation
    | TextObservation
    | DocumentObservation
    | FieldObservation,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


def _observation_refs(
    observation: StructureObservation,
) -> tuple[PhysicalSourceRef, ...]:
    if isinstance(
        observation,
        TabularObservation
        | TreeObservation
        | TextObservation
        | DocumentObservation
        | FieldObservation,
    ):
        return observation.source_refs
    if isinstance(observation, TabularShapeObservation):
        return (observation.table_ref,)
    if isinstance(observation, TreeShapeObservation):
        return (observation.root_ref,)
    if isinstance(observation, LogShapeObservation):
        return observation.line_refs
    return (*observation.block_refs, *observation.table_refs)


class StructureEvidence(FrozenContract):
    """Проверяемое наблюдение о физической структуре источника."""

    evidence_id: IdentifierStr
    code: IdentifierStr
    source_refs: tuple[PhysicalSourceRef, ...]
    observation: StructureObservation
    confidence: ConfidenceDecimal = Decimal("1")

    @model_validator(mode="after")
    def _validate_refs(self) -> Self:
        if not self.source_refs:
            raise ValueError("structure evidence требует physical provenance")
        _reject_duplicate_refs(self.source_refs, field="source_refs")
        if self.source_refs != _observation_refs(self.observation):
            raise ValueError("typed observation не совпадает с source_refs evidence")
        return self


class StructureCandidate(FrozenContract):
    """Ранжируемая гипотеза, которая сама по себе не разрешает execution."""

    candidate_id: IdentifierStr
    source: SourceArtifactRef
    extraction_fingerprint: FingerprintStr
    plan_kind: ParsePlanKind
    confidence: ConfidenceDecimal
    evidence: tuple[PhysicalSourceRef, ...]
    rationale_codes: tuple[IdentifierStr, ...] = ()
    observation_ids: Annotated[tuple[IdentifierStr, ...], Field(max_length=64)] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )

    assessment: AnalysisScore | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_evidence(self) -> Self:
        if self.assessment is not None and (
            self.assessment.confidence != self.confidence
            or self.assessment.observation_ids != self.observation_ids
        ):
            raise ValueError("candidate assessment не согласован с evidence/score")
        if not self.evidence:
            raise ValueError("structure candidate требует physical evidence")
        _reject_duplicate_refs(self.evidence, field="evidence")
        if len({ref.extraction_id for ref in self.evidence}) > 1:
            raise ValueError("structure candidate смешивает extraction runs")
        if len(self.rationale_codes) != len(set(self.rationale_codes)):
            raise ValueError("rationale_codes должны быть уникальны")
        if len(self.observation_ids) != len(set(self.observation_ids)):
            raise ValueError("Candidate observation IDs должны быть уникальны")
        return self


class StructureProfile(FrozenContract):
    """Fingerprint-bound профиль наблюдаемой физической структуры."""

    profile_id: IdentifierStr
    schema_version: SchemaVersionStr = "1.0.0"
    source: SourceArtifactRef
    extraction_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr
    producer: ProducerMetadata
    evidence: tuple[PhysicalSourceRef, ...]
    observations: tuple[StructureEvidence, ...]
    candidates: tuple[StructureCandidate, ...] = ()
    coverage: ProfileCoverage | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _validate_profile(self) -> Self:
        if self.schema_version not in {"1.0.0", "1.1.0"}:
            raise ValueError("Неподдерживаемая версия structural profile")
        if self.schema_version == "1.0.0" and self.coverage is not None:
            raise ValueError("Coverage требует profile schema 1.1.0")
        if self.schema_version == "1.0.0" and any(
            candidate.observation_ids for candidate in self.candidates
        ):
            raise ValueError("Candidate observation links требуют profile schema 1.1.0")
        if self.schema_version == "1.0.0" and any(
            isinstance(
                item.observation,
                TabularObservation
                | TreeObservation
                | TextObservation
                | DocumentObservation
                | FieldObservation,
            )
            for item in self.observations
        ):
            raise ValueError("Подробные observations требуют profile schema 1.1.0")
        if self.schema_version == "1.1.0":
            if self.coverage is None:
                raise ValueError("Profile 1.1.0 требует coverage")
            if (
                len(self.evidence) > 10_000
                or len(self.observations) > 2048
                or len(self.candidates) > 32
            ):
                raise ValueError("Profile превышает structural hard caps")
            expected = canonical_sha256_value(
                self,
                exclude_top_level=frozenset({"profile_fingerprint"}),
            )
            if self.profile_fingerprint == _AUTO_FINGERPRINT:
                object.__setattr__(self, "profile_fingerprint", expected)
            elif self.profile_fingerprint != expected:
                raise ValueError("Profile fingerprint не соответствует payload")
        if (not self.evidence or not self.observations) and not (
            self.schema_version == "1.1.0"
            and not self.evidence
            and not self.observations
            and not self.candidates
        ):
            raise ValueError("structure profile требует typed observations")
        _reject_duplicate_refs(self.evidence, field="evidence")
        if len({item.evidence_id for item in self.observations}) != len(
            self.observations
        ):
            raise ValueError("evidence_id должны быть уникальны")
        if self.schema_version == "1.1.0":
            observations_by_id = {item.evidence_id: item for item in self.observations}
            for candidate in self.candidates:
                if not candidate.observation_ids or any(
                    identifier not in observations_by_id
                    for identifier in candidate.observation_ids
                ):
                    raise ValueError("Candidate требует связанные observations")
                linked_refs = {
                    ref
                    for identifier in candidate.observation_ids
                    for ref in observations_by_id[identifier].source_refs
                }
                if not set(candidate.evidence) <= linked_refs:
                    raise ValueError(
                        "Candidate evidence выходит за связанные observations"
                    )
        observed_refs = {
            ref for observation in self.observations for ref in observation.source_refs
        }
        if observed_refs != set(self.evidence):
            raise ValueError("typed observations должны покрывать profile evidence")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("candidate_id должны быть уникальны")
        if any(candidate.source != self.source for candidate in self.candidates):
            raise ValueError("structure candidate относится к другому source")
        if any(
            candidate.extraction_fingerprint != self.extraction_fingerprint
            for candidate in self.candidates
        ):
            raise ValueError("structure candidate относится к другому extraction")
        if any(
            ref not in observed_refs
            for candidate in self.candidates
            for ref in candidate.evidence
        ):
            raise ValueError("structure candidate ссылается вне profile evidence")
        extraction_ids = {
            ref.extraction_id
            for ref in (
                *self.evidence,
                *(
                    ref
                    for observation in self.observations
                    for ref in observation.source_refs
                ),
                *(ref for candidate in self.candidates for ref in candidate.evidence),
            )
        }
        if len(extraction_ids) > 1:
            raise ValueError("structure profile смешивает extraction runs")
        return self


def _validate_profile_contract(profile: StructureProfile) -> None:
    StructureProfile.model_validate(profile.model_dump(mode="python"))


class RecordRangeRule(FrozenContract):
    """Безопасный диапазон физических записей с включёнными границами."""

    kind: Literal["record_range"] = "record_range"
    start_index: NonNegativeInt
    end_index: NonNegativeInt

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        if self.end_index < self.start_index:
            raise ValueError("end_index не может быть меньше start_index")
        return self


class GroupLinesRule(FrozenContract):
    """Декларативное объединение фиксированного числа соседних строк."""

    kind: Literal["group_lines"] = "group_lines"
    max_lines: PositiveInt


ParseRule = Annotated[
    RecordRangeRule | GroupLinesRule,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class TabularColumnSelector(FrozenContract):
    """Выбирает значение фиксированной физической колонки каждой data-row."""

    kind: Literal["tabular_column"] = "tabular_column"
    column_index: NonNegativeInt


class TreePathSelector(FrozenContract):
    """Выбирает name либо value по относительному пути внутри record root."""

    kind: Literal["tree_path"] = "tree_path"
    relative_path: tuple[IdentifierStr, ...] = ()
    value_source: Literal["node_value", "node_name"] = "node_value"
    steps: Annotated[tuple[TreeStep, ...], Field(max_length=30)] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        if self.steps and self.relative_path:
            raise ValueError("steps несовместим с legacy relative_path")
        return self


class LogTokenSelector(FrozenContract):
    """Выбирает token через закрытый безопасный delimiter без regex."""

    kind: Literal["log_token"] = "log_token"
    line_offset: NonNegativeInt = 0
    token_index: NonNegativeInt
    delimiter: Literal[
        "whitespace",
        "tab",
        "comma",
        "pipe",
        "colon",
        "equals",
    ] = "whitespace"


class DocumentTargetSelector(FrozenContract):
    """Выбирает закрытый тип extraction target блока либо таблицы документа."""

    kind: Literal["document_target"] = "document_target"
    target: Literal["block_text", "key", "value", "table_column"]
    column_index: NonNegativeInt | None = None
    block_offset: NonNegativeInt | None = None
    key_equals: IdentifierStr | None = None

    @model_validator(mode="after")
    def _validate_target(self) -> Self:
        if self.target == "table_column":
            if self.column_index is None:
                raise ValueError("table_column требует column_index")
            if self.block_offset is not None or self.key_equals is not None:
                raise ValueError("table_column не принимает block/key selectors")
            return self
        if self.column_index is not None or self.block_offset is None:
            raise ValueError("block target требует только block_offset")
        if (self.target == "value") != (self.key_equals is not None):
            raise ValueError("key_equals разрешён и обязателен только для value")
        return self


ParseFieldSelector = Annotated[
    TabularColumnSelector
    | TreePathSelector
    | LogTokenSelector
    | LogRecordSelector
    | DocumentTargetSelector,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class TabularRowGrouping(FrozenContract):
    """Создаёт одну semantic entity из фиксированного числа соседних строк."""

    kind: Literal["tabular_rows"] = "tabular_rows"
    rows_per_entity: PositiveInt = 1


class TreeNodeGrouping(FrozenContract):
    """Создаёт entity для узлов заданного record path."""

    kind: Literal["tree_nodes"] = "tree_nodes"
    record_path: tuple[IdentifierStr, ...] = ()
    include_descendants: StrictBool = False
    record_steps: Annotated[tuple[TreeStep, ...], Field(max_length=30)] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _validate_paths(self) -> Self:
        if self.record_steps and (self.record_path or self.include_descendants):
            raise ValueError("record_steps несовместим с legacy grouping")
        return self


class EveryLineStart(FrozenContract):
    """Начинает record на каждой доступной физической строке."""

    kind: Literal["every_line"] = "every_line"


class PrefixTokenStart(FrozenContract):
    """Начинает record при точном совпадении безопасного prefix token."""

    kind: Literal["prefix_token"] = "prefix_token"
    token: IdentifierStr
    delimiter: Literal[
        "whitespace",
        "tab",
        "comma",
        "pipe",
        "colon",
        "equals",
    ] = "whitespace"
    case_sensitive: StrictBool = True


LogRecordStart = Annotated[
    EveryLineStart | PrefixTokenStart,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class LogLineGrouping(FrozenContract):
    """Задаёт закрытую стратегию построения records из log-строк."""

    kind: Literal["log_lines"] = "log_lines"
    strategy: Literal["one_line", "fixed_lines", "blank_line"]
    start: LogRecordStart
    lines_per_record: PositiveInt | None = None

    @model_validator(mode="after")
    def _validate_strategy(self) -> Self:
        if (self.strategy == "fixed_lines") != (self.lines_per_record is not None):
            raise ValueError(
                "lines_per_record разрешён и обязателен только для fixed_lines"
            )
        return self


class DocumentBlockGrouping(FrozenContract):
    """Задаёт построение entity из одного или нескольких соседних блоков."""

    kind: Literal["document_blocks"] = "document_blocks"
    strategy: Literal["per_block", "contiguous_blocks"]
    max_blocks_per_entity: PositiveInt = 1

    @model_validator(mode="after")
    def _validate_strategy(self) -> Self:
        if self.strategy == "per_block" and self.max_blocks_per_entity != 1:
            raise ValueError("per_block допускает ровно один block на entity")
        return self


ParseEntityGrouping = Annotated[
    TabularRowGrouping
    | TreeNodeGrouping
    | LogLineGrouping
    | DocumentBlockGrouping
    | ExplicitRecordGrouping,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class ParseEntity(FrozenContract):
    """Полностью задаёт semantic entity, её поля, identity и parent relation."""

    entity_id: IdentifierStr
    entity_type: IdentifierStr
    field_ids: tuple[IdentifierStr, ...]
    grouping: ParseEntityGrouping
    identity_field_ids: tuple[IdentifierStr, ...] = ()
    parent_entity_id: IdentifierStr | None = None

    @model_validator(mode="after")
    def _validate_entity(self) -> Self:
        if not self.field_ids:
            raise ValueError("parse entity требует semantic fields")
        if len(self.field_ids) != len(set(self.field_ids)):
            raise ValueError("field_ids parse entity должны быть уникальны")
        if len(self.identity_field_ids) != len(set(self.identity_field_ids)):
            raise ValueError("identity_field_ids должны быть уникальны")
        if any(field not in self.field_ids for field in self.identity_field_ids):
            raise ValueError("identity field отсутствует в field_ids entity")
        if self.parent_entity_id == self.entity_id:
            raise ValueError("parse entity не может быть собственным parent")
        return self


class ParseField(FrozenContract):
    """Связь физического evidence с семантическим полем."""

    field_id: IdentifierStr
    semantic_name: IdentifierStr
    semantic_type: IdentifierStr
    source_refs: tuple[PhysicalSourceRef, ...]
    selector: ParseFieldSelector
    rules: tuple[ParseRule, ...] = ()

    @model_validator(mode="after")
    def _validate_source_refs(self) -> Self:
        if not self.source_refs:
            raise ValueError("parse field требует physical source references")
        _reject_duplicate_refs(self.source_refs, field="source_refs")
        if len({ref.extraction_id for ref in self.source_refs}) > 1:
            raise ValueError("parse field смешивает extraction runs")
        return self


class _ParsePlanBase(FrozenContract):
    plan_id: IdentifierStr
    schema_version: SchemaVersionStr = "1.0.0"
    revision: PositiveInt
    fingerprint: FingerprintStr = _AUTO_FINGERPRINT
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr
    confidence: ConfidenceDecimal
    producer: ProducerMetadata
    fields: Annotated[tuple[ParseField, ...], Field(min_length=1, max_length=1_024)]
    entities: Annotated[tuple[ParseEntity, ...], Field(min_length=1, max_length=32)]
    rules: tuple[ParseRule, ...] = ()
    evidence: tuple[PhysicalSourceRef, ...]
    analysis: PlanDerivation | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_base_plan(self) -> Self:
        extended = (
            self.analysis is not None
            or any(
                isinstance(f.selector, LogRecordSelector)
                or (isinstance(f.selector, TreePathSelector) and f.selector.steps)
                for f in self.fields
            )
            or any(
                isinstance(e.grouping, ExplicitRecordGrouping)
                or (
                    isinstance(e.grouping, TreeNodeGrouping) and e.grouping.record_steps
                )
                for e in self.entities
            )
            or bool(getattr(self, "record_steps", ()))
        )
        if self.schema_version not in {"1.0.0", "1.1.0"} or (
            extended and self.schema_version != "1.1.0"
        ):
            raise ValueError("неподдерживаемая версия ParsePlan")
        if self.analysis is not None:
            if (
                self.analysis.score.confidence != self.confidence
                or self.analysis.score.blockers
            ):
                raise ValueError("plan требует согласованный score без blockers")
            if self.analysis.unresolved_fields != tuple(
                f.field_id for f in self.fields if f.semantic_type == "unresolved"
            ):
                raise ValueError("unresolved fields не согласованы")
        if not self.fields:
            raise ValueError("parse plan требует хотя бы одно semantic field")
        if not self.entities:
            raise ValueError("parse plan требует хотя бы одну semantic entity")
        if not self.evidence:
            raise ValueError("parse plan требует physical evidence")
        if len({field.field_id for field in self.fields}) != len(self.fields):
            raise ValueError("field_id должны быть уникальны")
        if len({field.semantic_name for field in self.fields}) != len(self.fields):
            raise ValueError("semantic_name должны быть уникальны")
        if len({entity.entity_id for entity in self.entities}) != len(self.entities):
            raise ValueError("entity_id должны быть уникальны")
        declared_field_ids = tuple(field.field_id for field in self.fields)
        assigned_field_ids = tuple(
            field_id for entity in self.entities for field_id in entity.field_ids
        )
        if len(assigned_field_ids) != len(set(assigned_field_ids)):
            raise ValueError("semantic field назначен нескольким entities")
        if set(assigned_field_ids) != set(declared_field_ids):
            raise ValueError("каждый semantic field должен принадлежать одной entity")
        self._validate_entity_tree()
        _reject_duplicate_refs(self.evidence, field="evidence")
        refs = (
            *self.evidence,
            *(ref for field in self.fields for ref in field.source_refs),
        )
        if len({ref.extraction_id for ref in refs}) > 1:
            raise ValueError("parse plan смешивает extraction runs")
        expected_fingerprint = canonical_sha256_value(
            self,
            exclude_top_level=_FINGERPRINT_FIELDS,
        )
        if self.fingerprint == _AUTO_FINGERPRINT:
            object.__setattr__(self, "fingerprint", expected_fingerprint)
        elif self.fingerprint != expected_fingerprint:
            raise ValueError(
                "parse plan fingerprint не соответствует canonical payload"
            )
        return self

    def _validate_entity_tree(self) -> None:
        parents = {
            entity.entity_id: entity.parent_entity_id for entity in self.entities
        }
        if any(
            parent is not None and parent not in parents for parent in parents.values()
        ):
            raise ValueError("parse entity ссылается на отсутствующий parent")
        resolved: set[str] = set()
        for entity_id in parents:
            visited: set[str] = set()
            current: str | None = entity_id
            while current is not None and current not in resolved:
                if current in visited:
                    raise ValueError("parse entity relations не могут содержать cycle")
                visited.add(current)
                current = parents[current]
            resolved.update(visited)


def _validate_plan_fingerprint(plan: _ParsePlanBase) -> None:
    expected = canonical_sha256_value(
        plan,
        exclude_top_level=_FINGERPRINT_FIELDS,
    )
    if plan.fingerprint != expected:
        raise ValueError("parse plan fingerprint не соответствует canonical payload")
    type(plan).model_validate(plan.model_dump(mode="python"))


class TabularParsePlan(_ParsePlanBase):
    """Декларативно описывает разбор таблицы без execution callbacks."""

    kind: Literal["tabular"] = "tabular"
    table_ref: PhysicalSourceRef
    header_row: NonNegativeInt
    data_start_row: NonNegativeInt
    data_end_row: NonNegativeInt | None = None
    footer_start_row: NonNegativeInt | None = None
    repeated_header_rows: tuple[NonNegativeInt, ...] = ()

    @model_validator(mode="after")
    def _validate_rows(self) -> Self:
        if self.table_ref.kind is not PhysicalObjectKind.TABLE:
            raise ValueError("table_ref должен ссылаться на physical table")
        if self.table_ref.extraction_id != self.evidence[0].extraction_id:
            raise ValueError("table_ref относится к другому extraction")
        if self.data_start_row <= self.header_row:
            raise ValueError("data_start_row должен следовать после header_row")
        if self.data_end_row is not None and self.data_end_row < self.data_start_row:
            raise ValueError("data_end_row не может быть меньше data_start_row")
        if (
            self.footer_start_row is not None
            and self.footer_start_row <= self.data_start_row
        ):
            raise ValueError("footer_start_row должен следовать после начала data")
        if (
            self.data_end_row is not None
            and self.footer_start_row is not None
            and self.footer_start_row <= self.data_end_row
        ):
            raise ValueError("footer_start_row должен следовать после data_end_row")
        if len(self.repeated_header_rows) != len(set(self.repeated_header_rows)):
            raise ValueError("repeated_header_rows должны быть уникальны")
        if any(row < self.data_start_row for row in self.repeated_header_rows):
            raise ValueError("repeated header должен находиться внутри data region")
        if self.data_end_row is not None and any(
            row > self.data_end_row for row in self.repeated_header_rows
        ):
            raise ValueError("repeated header выходит за конец data region")
        if self.footer_start_row is not None and any(
            row >= self.footer_start_row for row in self.repeated_header_rows
        ):
            raise ValueError("repeated header не может находиться в footer region")
        if any(
            not isinstance(field.selector, TabularColumnSelector)
            for field in self.fields
        ):
            raise ValueError("tabular plan требует tabular field selectors")
        if any(
            ref.kind
            not in {
                PhysicalObjectKind.TABLE,
                PhysicalObjectKind.CELL,
                PhysicalObjectKind.VALUE,
            }
            for field in self.fields
            for ref in field.source_refs
        ):
            raise ValueError(
                "tabular field evidence должно ссылаться на table/cell/value"
            )
        if any(
            not isinstance(entity.grouping, TabularRowGrouping)
            for entity in self.entities
        ):
            raise ValueError("tabular plan требует tabular entity grouping")
        return self


class TreeParsePlan(_ParsePlanBase):
    """Декларативно описывает разбор дерева без execution callbacks."""

    kind: Literal["tree"] = "tree"
    root_ref: PhysicalSourceRef
    record_path: tuple[IdentifierStr, ...] = ()
    record_steps: Annotated[tuple[TreeStep, ...], Field(max_length=30)] = Field(
        default=(), exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def _validate_root_ref(self) -> Self:
        if self.record_steps and self.record_path:
            raise ValueError("record_steps несовместим с legacy record_path")
        if any(
            isinstance(e.grouping, TreeNodeGrouping)
            and e.parent_entity_id is None
            and e.grouping.record_steps != self.record_steps
            for e in self.entities
        ):
            raise ValueError("root grouping steps не совпадают с plan")
        if self.root_ref.kind is not PhysicalObjectKind.TREE_NODE:
            raise ValueError("root_ref должен ссылаться на physical tree node")
        if self.root_ref.extraction_id != self.evidence[0].extraction_id:
            raise ValueError("root_ref относится к другому extraction")
        if any(
            not isinstance(field.selector, TreePathSelector) for field in self.fields
        ):
            raise ValueError("tree plan требует tree path selectors")
        if any(
            ref.kind not in {PhysicalObjectKind.TREE_NODE, PhysicalObjectKind.VALUE}
            for field in self.fields
            for ref in field.source_refs
        ):
            raise ValueError("tree field evidence должно ссылаться на node/value")
        if any(
            not isinstance(entity.grouping, TreeNodeGrouping)
            for entity in self.entities
        ):
            raise ValueError("tree plan требует tree node grouping")
        if any(
            isinstance(entity.grouping, TreeNodeGrouping)
            and entity.parent_entity_id is None
            and entity.grouping.record_path != self.record_path
            for entity in self.entities
        ):
            raise ValueError(
                "root tree entity grouping должен совпадать с record_path plan"
            )
        return self


class LogParsePlan(_ParsePlanBase):
    """Декларативно описывает группировку строк без execution callbacks."""

    kind: Literal["log"] = "log"
    line_refs: tuple[PhysicalSourceRef, ...]
    max_lines_per_record: PositiveInt

    @model_validator(mode="after")
    def _validate_line_refs(self) -> Self:
        if not self.line_refs:
            raise ValueError("log plan требует line_refs")
        _reject_duplicate_refs(self.line_refs, field="line_refs")
        if any(ref.kind is not PhysicalObjectKind.LINE for ref in self.line_refs):
            raise ValueError("line_refs должны ссылаться на physical lines")
        if any(
            ref.extraction_id != self.evidence[0].extraction_id
            for ref in self.line_refs
        ):
            raise ValueError("line_refs относятся к другому extraction")
        if any(
            not isinstance(field.selector, LogTokenSelector | LogRecordSelector)
            for field in self.fields
        ):
            raise ValueError("log plan требует safe token selectors")
        if any(
            ref.kind not in {PhysicalObjectKind.LINE, PhysicalObjectKind.VALUE}
            for field in self.fields
            for ref in field.source_refs
        ):
            raise ValueError("log field evidence должно ссылаться на line/value")
        if any(
            not isinstance(entity.grouping, LogLineGrouping | ExplicitRecordGrouping)
            for entity in self.entities
        ):
            raise ValueError("log plan требует log line grouping")
        if any(
            entity.grouping.lines_per_record is not None
            and entity.grouping.lines_per_record > self.max_lines_per_record
            for entity in self.entities
            if isinstance(entity.grouping, LogLineGrouping)
        ):
            raise ValueError("entity grouping превышает max_lines_per_record")
        _validate_explicit_records(
            self.entities, self.fields, self.line_refs, self.max_lines_per_record
        )
        fields_by_id = {field.field_id: field for field in self.fields}
        for entity in self.entities:
            grouping = entity.grouping
            if not isinstance(grouping, LogLineGrouping):
                continue
            if grouping.strategy == "one_line":
                grouping_limit = 1
            elif grouping.strategy == "fixed_lines":
                if grouping.lines_per_record is None:
                    raise ValueError("fixed_lines требует lines_per_record")
                grouping_limit = grouping.lines_per_record
            else:
                grouping_limit = self.max_lines_per_record
            for field_id in entity.field_ids:
                selector = fields_by_id[field_id].selector
                if (
                    isinstance(selector, LogTokenSelector)
                    and selector.line_offset >= grouping_limit
                ):
                    raise ValueError(
                        "log selector line_offset выходит за record boundary"
                    )
        grouping_starts = tuple(
            entity.grouping.start.canonical_json()
            for entity in self.entities
            if isinstance(entity.grouping, LogLineGrouping)
        )
        if len(grouping_starts) != len(set(grouping_starts)):
            raise ValueError("log record variants требуют уникальные start conditions")
        if len(self.entities) > 1 and any(
            isinstance(entity.grouping, LogLineGrouping)
            and isinstance(entity.grouping.start, EveryLineStart)
            for entity in self.entities
        ):
            raise ValueError("every_line несовместим с несколькими record variants")
        return self


class DocumentParsePlan(_ParsePlanBase):
    """Декларативно описывает разбор блоков без execution callbacks."""

    kind: Literal["document"] = "document"
    block_refs: tuple[PhysicalSourceRef, ...]

    @model_validator(mode="after")
    def _validate_block_refs(self) -> Self:
        if not self.block_refs:
            raise ValueError("document plan требует block_refs")
        _reject_duplicate_refs(self.block_refs, field="block_refs")
        if any(ref.kind is not PhysicalObjectKind.BLOCK for ref in self.block_refs):
            raise ValueError("block_refs должны ссылаться на physical blocks")
        if any(
            ref.extraction_id != self.evidence[0].extraction_id
            for ref in self.block_refs
        ):
            raise ValueError("block_refs относятся к другому extraction")
        if any(
            not isinstance(field.selector, DocumentTargetSelector)
            for field in self.fields
        ):
            raise ValueError("document plan требует document target selectors")
        if any(
            ref.kind
            not in {
                PhysicalObjectKind.BLOCK,
                PhysicalObjectKind.TABLE,
                PhysicalObjectKind.CELL,
                PhysicalObjectKind.VALUE,
            }
            for field in self.fields
            for ref in field.source_refs
        ):
            raise ValueError(
                "document field evidence должно ссылаться на block/table/cell/value"
            )
        if any(
            not isinstance(
                entity.grouping, DocumentBlockGrouping | ExplicitRecordGrouping
            )
            for entity in self.entities
        ):
            raise ValueError("document plan требует document block grouping")
        _validate_explicit_records(self.entities, self.fields, self.block_refs, 64)
        fields_by_id = {field.field_id: field for field in self.fields}
        for entity in self.entities:
            grouping = entity.grouping
            if not isinstance(grouping, DocumentBlockGrouping):
                continue
            for field_id in entity.field_ids:
                selector = fields_by_id[field_id].selector
                if (
                    isinstance(selector, DocumentTargetSelector)
                    and selector.block_offset is not None
                    and selector.block_offset >= grouping.max_blocks_per_entity
                ):
                    raise ValueError(
                        "document selector block_offset выходит за entity boundary"
                    )
        return self


def _validate_explicit_records(
    entities: tuple[ParseEntity, ...],
    fields: tuple[ParseField, ...],
    scope: tuple[PhysicalSourceRef, ...],
    maximum: int,
) -> None:
    explicit = [e for e in entities if isinstance(e.grouping, ExplicitRecordGrouping)]
    if not explicit:
        return
    if len(explicit) != len(entities):
        raise ValueError("explicit grouping нельзя смешивать с legacy grouping")
    refs: list[PhysicalSourceRef] = []
    for entity in explicit:
        grouping = entity.grouping
        if not isinstance(grouping, ExplicitRecordGrouping):
            continue
        for record in grouping.records:
            if len(record) > maximum:
                raise ValueError("record превышает boundary limit")
            refs.extend(record)
            for field in fields:
                if field.field_id not in entity.field_ids:
                    continue
                selector = field.selector
                offset = (
                    selector.block_offset
                    if isinstance(selector, DocumentTargetSelector)
                    else selector.line_offset
                    if isinstance(selector, LogTokenSelector)
                    else None
                )
                if offset is not None and offset >= len(record):
                    raise ValueError("selector выходит за explicit record")
    if len(refs) != len(set(refs)) or set(refs) != set(scope):
        raise ValueError("explicit records должны покрывать scope ровно один раз")


ParsePlan = Annotated[
    TabularParsePlan | TreeParsePlan | LogParsePlan | DocumentParsePlan,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


def _iter_plan_refs(plan: ParsePlan) -> tuple[PhysicalSourceRef, ...]:
    field_refs = tuple(ref for field in plan.fields for ref in field.source_refs)
    variant_refs: tuple[PhysicalSourceRef, ...]
    if isinstance(plan, TabularParsePlan):
        variant_refs = (plan.table_ref,)
    elif isinstance(plan, TreeParsePlan):
        variant_refs = (plan.root_ref,)
    elif isinstance(plan, LogParsePlan):
        variant_refs = plan.line_refs
    else:
        variant_refs = plan.block_refs
    return (*plan.evidence, *field_refs, *variant_refs)


class PhysicalSample(FrozenContract):
    """Bounded raw sample с location и fingerprint конкретного parser batch."""

    source_ref: PhysicalSourceRef
    batch_fingerprint: FingerprintStr
    raw_value: RawScalar
    location: SourceLocation
    fingerprint: FingerprintStr = _AUTO_FINGERPRINT

    @model_validator(mode="after")
    def _bind_fingerprint(self) -> Self:
        if self.source_ref.kind is PhysicalObjectKind.TABLE:
            raise ValueError("raw sample не может представлять table как scalar")
        expected = canonical_sha256_value(
            self,
            exclude_top_level=_FINGERPRINT_FIELDS,
        )
        if self.fingerprint == _AUTO_FINGERPRINT:
            object.__setattr__(self, "fingerprint", expected)
        elif self.fingerprint != expected:
            raise ValueError("physical sample fingerprint не соответствует payload")
        return self


def _validate_sample_fingerprint(sample: PhysicalSample) -> None:
    expected = canonical_sha256_value(
        sample,
        exclude_top_level=_FINGERPRINT_FIELDS,
    )
    if sample.fingerprint != expected:
        raise ValueError("physical sample fingerprint не соответствует payload")
    PhysicalSample.model_validate(sample.model_dump(mode="python"))


def _validate_issue_refs(
    issues: tuple[ValidationIssue, ...],
    *,
    allowed_refs: tuple[PhysicalSourceRef, ...],
    context: str,
) -> None:
    known_refs = set(allowed_refs)
    if any(ref not in known_refs for issue in issues for ref in issue.source_refs):
        raise ValueError(f"{context} содержит source reference вне bound snapshot")


class StructureAnalysisRequest(FrozenContract):
    """Snapshot-bound запрос с bounded raw sample одного extraction run."""

    source: SourceArtifactRef
    manifest: ExtractedDatasetManifest
    profile: StructureProfile
    mode: SemanticParsingMode = SemanticParsingMode.DETERMINISTIC
    samples: Annotated[
        tuple[PhysicalSample, ...],
        Field(min_length=1, max_length=_MAX_SAMPLE_VALUES),
    ]
    max_sample_values: Annotated[PositiveInt, Field(le=_MAX_SAMPLE_VALUES)] = 100
    max_sample_bytes: Annotated[PositiveInt, Field(le=_MAX_SAMPLE_BYTES)] = 65_536

    @property
    def sample_refs(self) -> tuple[PhysicalSourceRef, ...]:
        """Вернуть refs в том же порядке, что и bounded samples."""

        return tuple(sample.source_ref for sample in self.samples)

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        _validate_profile_contract(self.profile)
        if self.source != self.manifest.source or self.source != self.profile.source:
            raise ValueError("source references анализа не согласованы")
        if self.profile.extraction_fingerprint != self.manifest.extraction_fingerprint:
            raise ValueError("profile не относится к extraction manifest")
        if not self.samples:
            raise ValueError("analysis request требует bounded physical samples")
        if len(self.samples) > self.max_sample_values:
            raise ValueError("physical samples превышают max_sample_values")
        _reject_duplicate_refs(self.sample_refs, field="sample_refs")
        serialized_size = sum(
            len(sample.canonical_json().encode("utf-8")) for sample in self.samples
        )
        if serialized_size > self.max_sample_bytes:
            raise ValueError("physical samples превышают max_sample_bytes")
        known_batch_indices = {batch.batch_index for batch in self.manifest.batches}
        batch_fingerprints = {
            batch.batch_index: batch.batch_fingerprint
            for batch in self.manifest.batches
        }
        known_refs = set(self.manifest.source_index.refs)
        candidate_refs = tuple(
            ref for candidate in self.profile.candidates for ref in candidate.evidence
        )
        for ref in (
            *self.profile.evidence,
            *candidate_refs,
            *self.sample_refs,
            *self.manifest.source_index.refs,
        ):
            if ref.extraction_id != self.manifest.extraction_id:
                raise ValueError("physical reference относится к другому extraction")
            if ref.batch_index not in known_batch_indices:
                raise ValueError("physical reference относится к отсутствующему batch")
        if any(
            ref not in known_refs
            for ref in (*self.profile.evidence, *candidate_refs, *self.sample_refs)
        ):
            raise ValueError(
                "analysis request содержит отсутствующий physical reference"
            )
        for sample in self.samples:
            _validate_sample_fingerprint(sample)
            if (
                sample.batch_fingerprint
                != batch_fingerprints[sample.source_ref.batch_index]
            ):
                raise ValueError("sample относится к другому parser batch fingerprint")
            if sample.location.source != self.source:
                raise ValueError("sample location относится к другому source snapshot")
        return self


class StructurePlanCreated(FrozenContract):
    """Результат анализа с созданным, но ещё не проверенным ``ParsePlan``.

    ``profile`` и ``plan`` должны относиться к одному source, extraction и
    profile fingerprint.
    """

    kind: Literal["plan_created"] = "plan_created"
    profile: StructureProfile
    plan: ParsePlan

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        _validate_profile_contract(self.profile)
        _validate_plan_fingerprint(self.plan)
        if self.plan.source_fingerprint != self.profile.source.source_fingerprint:
            raise ValueError("created plan относится к другому source")
        if self.plan.extraction_fingerprint != self.profile.extraction_fingerprint:
            raise ValueError("created plan относится к другому extraction")
        if self.plan.profile_fingerprint != self.profile.profile_fingerprint:
            raise ValueError("created plan относится к другому profile")
        return self


class StructureNeedsReview(FrozenContract):
    """Результат анализа с вариантами, требующими внешней проверки.

    Кандидаты должны принадлежать ``profile``; пустые candidates или issues
    являются недопустимым состоянием.
    """

    kind: Literal["needs_review"] = "needs_review"
    profile: StructureProfile
    candidates: tuple[StructureCandidate, ...]
    issues: tuple[ValidationIssue, ...]

    @model_validator(mode="after")
    def _validate_review(self) -> Self:
        _validate_profile_contract(self.profile)
        if not self.candidates or not self.issues:
            raise ValueError("needs_review требует candidates и issues")
        if len({candidate.candidate_id for candidate in self.candidates}) != len(
            self.candidates
        ):
            raise ValueError("review candidates должны быть уникальны")
        if any(
            candidate not in self.profile.candidates for candidate in self.candidates
        ):
            raise ValueError("review candidate отсутствует в structure profile")
        if any(
            candidate.source != self.profile.source for candidate in self.candidates
        ):
            raise ValueError("review candidate относится к другому source")
        if any(
            candidate.extraction_fingerprint != self.profile.extraction_fingerprint
            for candidate in self.candidates
        ):
            raise ValueError("review candidate относится к другому extraction")
        _validate_issue_refs(
            self.issues,
            allowed_refs=self.profile.evidence,
            context="structure review",
        )
        return self


class StructureRejected(FrozenContract):
    """Сообщает об отклонении анализа структуры с typed issues.

    Результат не содержит ``ParsePlan`` и требует хотя бы одну issue.
    """

    kind: Literal["rejected"] = "rejected"
    profile: StructureProfile
    issues: tuple[ValidationIssue, ...]

    @model_validator(mode="after")
    def _validate_rejection(self) -> Self:
        _validate_profile_contract(self.profile)
        if not self.issues:
            raise ValueError("rejected outcome требует issues")
        _validate_issue_refs(
            self.issues,
            allowed_refs=self.profile.evidence,
            context="structure rejection",
        )
        return self


class StructureNeedsSemanticAnalysis(FrozenContract):
    """Правил или проверенного physical scope недостаточно; LLM не вызывается."""

    kind: Literal["needs_semantic_analysis"] = "needs_semantic_analysis"
    code: Literal["NEEDS_SEMANTIC_ANALYSIS"] = "NEEDS_SEMANTIC_ANALYSIS"
    profile: StructureProfile
    issues: Annotated[tuple[ValidationIssue, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        _validate_profile_contract(self.profile)
        _validate_issue_refs(
            self.issues, allowed_refs=self.profile.evidence, context="semantic analysis"
        )
        return self


StructureAnalysisResult = Annotated[
    StructurePlanCreated
    | StructureNeedsReview
    | StructureRejected
    | StructureNeedsSemanticAnalysis,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]


class ParsePlanValidationRequest(FrozenContract):
    """Данные для проверки plan против конкретного extraction snapshot."""

    plan: ParsePlan
    source: SourceArtifactRef
    manifest: ExtractedDatasetManifest
    profile: StructureProfile

    @model_validator(mode="after")
    def _validate_lineage(self) -> Self:
        _validate_profile_contract(self.profile)
        _validate_plan_fingerprint(self.plan)
        if self.source != self.manifest.source or self.source != self.profile.source:
            raise ValueError("source references validation request не согласованы")
        if self.plan.source_fingerprint != self.source.source_fingerprint:
            raise ValueError("parse plan относится к другому source fingerprint")
        if self.plan.extraction_fingerprint != self.manifest.extraction_fingerprint:
            raise ValueError("parse plan относится к другому extraction fingerprint")
        if self.profile.extraction_fingerprint != self.manifest.extraction_fingerprint:
            raise ValueError("structure profile относится к другому extraction")
        if self.plan.profile_fingerprint != self.profile.profile_fingerprint:
            raise ValueError("parse plan относится к другому structure profile")

        refs = (
            *_iter_plan_refs(self.plan),
            *self.profile.evidence,
            *(
                ref
                for candidate in self.profile.candidates
                for ref in candidate.evidence
            ),
        )
        if any(ref.extraction_id != self.manifest.extraction_id for ref in refs):
            raise ValueError("parse plan содержит reference другого extraction")
        known_batch_indices = {batch.batch_index for batch in self.manifest.batches}
        if any(ref.batch_index not in known_batch_indices for ref in refs):
            raise ValueError("parse plan содержит reference отсутствующего batch")
        known_refs = set(self.manifest.source_index.refs)
        if any(ref not in known_refs for ref in refs):
            raise ValueError("parse plan содержит отсутствующий physical reference")
        return self


class ValidatedParsePlan(FrozenContract):
    """Результат принятой проверки; не является security capability."""

    plan: ParsePlan
    validator_id: IdentifierStr
    validator_version: VersionStr
    validated_at: UtcDateTime
    validation_fingerprint: FingerprintStr
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr
    plan_fingerprint: FingerprintStr
    decision: Literal[ValidationDecision.ACCEPTED] = ValidationDecision.ACCEPTED
    issues: tuple[ValidationIssue, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> Self:
        _validate_plan_fingerprint(self.plan)
        expected = (
            self.plan.source_fingerprint,
            self.plan.extraction_fingerprint,
            self.plan.profile_fingerprint,
            self.plan.fingerprint,
        )
        actual = (
            self.source_fingerprint,
            self.extraction_fingerprint,
            self.profile_fingerprint,
            self.plan_fingerprint,
        )
        if actual != expected:
            raise ValueError("validated plan fingerprints не согласованы с plan")
        if any(
            issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
            for issue in self.issues
        ):
            raise ValueError("accepted plan не может содержать error issues")
        _validate_issue_refs(
            self.issues,
            allowed_refs=_iter_plan_refs(self.plan),
            context="validated parse plan",
        )
        return self


class ParsePlanValidationResult(FrozenContract):
    """Outcome проверки ParsePlan с явным accepted/rejected состоянием."""

    validator_id: IdentifierStr
    validator_version: VersionStr
    validated_at: UtcDateTime
    validation_fingerprint: FingerprintStr
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr
    plan_fingerprint: FingerprintStr
    decision: ValidationDecision
    issues: tuple[ValidationIssue, ...] = ()
    validated_plan: ValidatedParsePlan | None = None

    @model_validator(mode="after")
    def _validate_outcome(self) -> Self:
        has_errors = any(
            issue.severity in {IssueSeverity.ERROR, IssueSeverity.CRITICAL}
            for issue in self.issues
        )
        if self.decision is ValidationDecision.ACCEPTED:
            if self.validated_plan is None or has_errors:
                raise ValueError("accepted result требует validated plan без ошибок")
            evidence = self.validated_plan
            ValidatedParsePlan.model_validate(evidence.model_dump(mode="python"))
            if (
                self.validator_id,
                self.validator_version,
                self.validated_at,
                self.validation_fingerprint,
                self.source_fingerprint,
                self.extraction_fingerprint,
                self.profile_fingerprint,
                self.plan_fingerprint,
                self.issues,
            ) != (
                evidence.validator_id,
                evidence.validator_version,
                evidence.validated_at,
                evidence.validation_fingerprint,
                evidence.source_fingerprint,
                evidence.extraction_fingerprint,
                evidence.profile_fingerprint,
                evidence.plan_fingerprint,
                evidence.issues,
            ):
                raise ValueError("validation result не совпадает с accepted evidence")
        else:
            if self.validated_plan is not None:
                raise ValueError(
                    "только accepted result может содержать validated plan"
                )
            if not self.issues:
                raise ValueError("non-accepted result требует issues")
            if any(issue.source_refs for issue in self.issues):
                raise ValueError(
                    "non-accepted result не может доказать source references"
                )
        return self


class ParseExecutionContext(FrozenContract):
    """Ограничения чистого применения заранее проверенного ParsePlan."""

    run_id: IdentifierStr
    source_fingerprint: FingerprintStr
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    manifest: ExtractedDatasetManifest
    max_records_per_batch: PositiveInt = 1_000
    profile: StructureProfile | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_manifest(self) -> Self:
        if self.source_fingerprint != self.manifest.source.source_fingerprint:
            raise ValueError("execution context относится к другому source")
        if self.extraction_fingerprint != self.manifest.extraction_fingerprint:
            raise ValueError("execution context относится к другому extraction")
        if self.profile is not None and (
            self.profile.source != self.manifest.source
            or self.profile.extraction_fingerprint != self.extraction_fingerprint
        ):
            raise ValueError("execution profile относится к другому extraction")
        return self
