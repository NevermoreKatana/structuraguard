"""Версионированные результаты ранжирования; кандидаты не разрешают загрузку."""

from collections.abc import Iterator
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BeforeValidator, Field, StrictBool, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import (
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PositiveInt,
)
from .database import CatalogColumnRef
from .mapping import MappingCandidate
from .normalized import SemanticFieldRef


def _bounded_decimal(value: object) -> object:
    if isinstance(value, float | bool):
        raise ValueError("Score требует Decimal либо десятичную строку")
    if isinstance(value, str):
        if len(value) > 48:
            raise ValueError("Score превышает numeric budget")
        # Pydantic проверит синтаксис, этот guard ограничивает allocation.
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or value.__sizeof__() > 512:
            raise ValueError("Score превышает numeric budget")
        parts = value.as_tuple()
        if (
            len(parts.digits) > 18
            or not isinstance(parts.exponent, int)
            or abs(parts.exponent) > 18
        ):
            raise ValueError("Score превышает numeric budget")
    return value


def _checked_decimal(value: Decimal) -> Decimal:
    _bounded_decimal(value)
    return value


Score = Annotated[
    Decimal,
    BeforeValidator(_bounded_decimal),
    Field(ge=0, le=1, allow_inf_nan=False),
    AfterValidator(_checked_decimal),
]
SignalCode = Literal[
    "exact_name",
    "normalized_name",
    "transliterated_name",
    "name_similarity",
    "alias_match",
    "type_compatibility",
    "value_pattern_match",
    "structural_context",
    "database_relation_score",
]
CompatibilityStatus = Literal["compatible", "conditional", "unknown", "incompatible"]


class SensitiveMappingContract(FrozenContract):
    """Имена и references разрешены в явной serialization, скрыты в repr."""

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())


class MappingWeights(FrozenContract):
    """Настроить шесть неотрицательных весов с точной единичной суммой.

    Значения задаются Decimal или десятичными строками; float, bool, NaN/Infinity
    и слишком большие коэффициенты/exponents отклоняются. Недоступный сигнал
    не перераспределяет свой вес. Name diagnostics отдельно не суммируются.

    Raises:
        pydantic.ValidationError: Нарушены числовые ограничения или сумма весов.

    Side effects:
        Нет I/O или обучения; результат immutable. Веса не снимают type/policy
        запреты и blockers, даже если дают кандидату score 1.
    """

    name_similarity: Score = Decimal("0.35")
    alias_match: Score = Decimal("0.20")
    type_compatibility: Score = Decimal("0.20")
    value_pattern_match: Score = Decimal("0.10")
    structural_context: Score = Decimal("0.10")
    database_relation_score: Score = Decimal("0.05")

    @model_validator(mode="after")
    def _sum(self) -> Self:
        with localcontext(Context(prec=80, rounding=ROUND_HALF_EVEN)):
            if (
                sum(
                    (
                        self.name_similarity,
                        self.alias_match,
                        self.type_compatibility,
                        self.value_pattern_match,
                        self.structural_context,
                        self.database_relation_score,
                    )
                )
                != 1
            ):
                raise ValueError("Сумма weights должна быть равна 1")
        return self


class DeterministicMappingOptions(FrozenContract):
    """Ограничения одного вызова; I/O и внешние providers не конфигурируются.

    Decimal задаётся объектом Decimal либо строкой; float запрещён. Budgets
    допускают только сужение опубликованных ceilings. Пустой evidence не
    перераспределяет weights. Время и process locale не влияют на результат.

    Attributes:
        weights: Шесть проверенных весов; по умолчанию MappingWeights().
        top_k: От 1 до 10 кандидатов на поле, по умолчанию 5.
        auto_threshold: Включительная граница auto_candidate, по умолчанию 0.90.
        review_threshold: Включительная граница review, по умолчанию 0.70.
        ambiguity_margin: Положительный минимальный gap; по умолчанию 0.10.
        max_operations: Консервативный предел evidence work, включая FK/identity
            обходы; не время в секундах. Остальные max_* ограничивают shape/allocations.

    Raises:
        pydantic.ValidationError: Неверные weights, thresholds, ceilings или extra
            поля; llm_weight и provider не входят в контракт.

    Side effects:
        Нет I/O; конфигурация immutable и не авторизует загрузку данных.
    """

    algorithm: Literal["deterministic_mapping_v1"] = "deterministic_mapping_v1"
    weights: MappingWeights = Field(default_factory=MappingWeights)
    top_k: Annotated[PositiveInt, Field(le=10)] = 5
    auto_threshold: Score = Decimal("0.90")
    review_threshold: Score = Decimal("0.70")
    ambiguity_margin: Score = Decimal("0.10")
    ambiguity_penalty: Score = Decimal("0.10")
    collision_penalty: Score = Decimal("0.10")
    max_fields: Annotated[PositiveInt, Field(le=1024)] = 1024
    max_columns: Annotated[PositiveInt, Field(le=10000)] = 10000
    max_edges: Annotated[PositiveInt, Field(le=20000)] = 20000
    max_relationships: Annotated[PositiveInt, Field(le=4096)] = 4096
    max_aliases: Annotated[PositiveInt, Field(le=10000)] = 10000
    max_name_bytes: Annotated[PositiveInt, Field(le=256)] = 256
    max_tokens: Annotated[PositiveInt, Field(le=32)] = 32
    max_pairs: Annotated[PositiveInt, Field(le=2000000)] = 2000000
    max_operations: Annotated[PositiveInt, Field(le=10000000)] = 10000000
    max_input_bytes: Annotated[PositiveInt, Field(le=16777216)] = 16777216
    max_state_bytes: Annotated[PositiveInt, Field(le=33554432)] = 33554432
    max_result_bytes: Annotated[PositiveInt, Field(le=8388608)] = 8388608

    @model_validator(mode="after")
    def _thresholds(self) -> Self:
        if self.review_threshold >= self.auto_threshold or self.ambiguity_margin == 0:
            raise ValueError("Некорректные thresholds/margin")
        return self


class MappingScope(SensitiveMappingContract):
    """Ограничить targets точными refs; deny сужает allow, пустой allow запрещает всё.

    Attributes:
        target_id: Идентификатор DB target, совпадающий с catalog.target_id.
        target_policy_fingerprint: Binding к inspection policy; не доказательство grants.
        allow: Разрешённые CatalogColumnRef, без wildcard или нормализации имён.
        deny: Исключения из allow; порядок и дубли канонизируются как у allow.

    Raises:
        pydantic.ValidationError: Некорректный DTO. Существование refs и согласованность
            с каталогом проверяются в rank через MappingError.

    Side effects:
        Нет DB calls. Caller формирует scope из своей policy; semantic hints
        не могут расширить этот scope. Repr скрыт, явная serialization чувствительна.
    """

    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    allow: Annotated[tuple[CatalogColumnRef, ...], Field(max_length=10000)]
    deny: Annotated[tuple[CatalogColumnRef, ...], Field(max_length=10000)] = ()

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        for name in ("allow", "deny"):
            values = getattr(self, name)
            object.__setattr__(
                self,
                name,
                tuple(sorted(set(values), key=lambda r: (r.table_id, r.column_id))),
            )
        return self

    @property
    def fingerprint(self) -> str:
        """Отпечаток selectors отдельно от inspection policy."""
        return canonical_sha256_value(self)


class CandidateSignal(SensitiveMappingContract):
    """Опубликовать числовой сигнал, его вес и вклад в SDK score.

    Attributes:
        code: Закрытый идентификатор сигнала; exact/normalized/transliterated
            diagnostics имеют вес 0, aggregate name_similarity учитывается один раз.
        value: Значение от 0 до 1; для недоступного evidence равно 0.
        weight: Настроенный вес; отсутствие evidence не меняет его.
        contribution: Числовой вклад в base_score, вычисленный SDK.
        available: False при отсутствии evidence; нулевой доступный сигнал — несовпадение.
        coverage: Для pattern signal доля проверенных non-null occurrences.
        reasons: Machine-readable codes, без raw values и alias text.

    Создание immutable DTO не выполняет I/O; ошибочные значения дают
    pydantic.ValidationError. DTO описывает evidence и не подтверждает grants.
    """

    code: SignalCode
    value: Score
    weight: Score
    contribution: Score
    available: StrictBool = True
    coverage: Score | None = None
    reasons: tuple[IdentifierStr, ...] = ()


class CandidateExplanation(SensitiveMappingContract):
    """Объяснить SDK score кандидата без raw examples и comments.

    candidate_id связывает explanation с MappingCandidate. base_score — сумма
    contributions; final_score учитывает явные penalties и совпадает с confidence.
    compatibility различает compatible, conditional, unknown и исключаемый incompatible
    evidence; blockers запрещают auto даже при высоком score. foreign_key_ids и
    identity_evidence — структурные hints, не проверка значений или стратегии load.

    DTO immutable и без I/O; неверные поля дают pydantic.ValidationError.
    Refs/hashes чувствительны при serialization, repr скрыт.
    """

    candidate_id: IdentifierStr
    base_score: Score
    final_score: Score
    signals: tuple[CandidateSignal, ...]
    compatibility: CompatibilityStatus
    ambiguity_penalty: Score = Decimal("0")
    validation_penalty: Score = Decimal("0")
    security_penalty: Score = Decimal("0")
    blockers: tuple[IdentifierStr, ...] = ()
    foreign_key_ids: tuple[IdentifierStr, ...] = ()
    identity_evidence: tuple[IdentifierStr, ...] = ()


class FieldCandidates(SensitiveMappingContract):
    """Сохранить top-k, status и ambiguity одного SemanticFieldRef.

    candidates и explanations связаны по ID и порядку; targets не повторяются.
    status: auto_candidate — рекомендация без blockers, review — ручная проверка,
    rejected — недостаточный score, unmapped — пустой список с reasons.
    gap — разница лучших base scores до top-k либо None при отсутствии runner-up.
    competitor_count/tie_count также считаются до pruning. ambiguous отдельно от
    status и сохраняет скрытого runner-up при k=1; это не глобальное назначение полей.

    DTO immutable, без I/O; нарушения bindings дают pydantic.ValidationError.
    Даже auto_candidate не является MappingPlan или разрешением на импорт.
    """

    source: SemanticFieldRef
    candidates: Annotated[tuple[MappingCandidate, ...], Field(max_length=10)]
    explanations: Annotated[tuple[CandidateExplanation, ...], Field(max_length=10)]
    status: Literal["auto_candidate", "review", "rejected", "unmapped"]
    ambiguous: StrictBool
    gap: Score | None
    competitor_count: NonNegativeInt
    tie_count: NonNegativeInt = 0
    reasons: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if tuple(c.candidate_id for c in self.candidates) != tuple(
            e.candidate_id for e in self.explanations
        ):
            raise ValueError("Explanation binding mismatch")
        if any(c.source != self.source for c in self.candidates):
            raise ValueError("Candidate source mismatch")
        if len({c.target for c in self.candidates}) != len(self.candidates):
            raise ValueError("Duplicate candidate target")
        if (self.status == "unmapped") != (not self.candidates):
            raise ValueError("Unmapped state mismatch")
        if self.ambiguous and self.status == "auto_candidate":
            raise ValueError("Ambiguity запрещает auto recommendation")
        if self.competitor_count < len(self.candidates):
            raise ValueError("Candidate count mismatch")
        if any(
            c.confidence != e.final_score
            for c, e in zip(self.candidates, self.explanations, strict=True)
        ):
            raise ValueError("Candidate score mismatch")
        return self


class MappingSafeSummary(FrozenContract):
    """Представить counters и classification для логирования.

    field_count/candidate_count отражают возвращённый top-k;
    ambiguous_count/unmapped_count — состояния полей. Names, refs, hashes,
    samples и aliases отсутствуют. Создаётся result.safe_summary() без I/O;
    не редактирует полный result и не меняет классификацию исходного профиля.
    """

    field_count: NonNegativeInt
    candidate_count: NonNegativeInt
    ambiguous_count: NonNegativeInt
    unmapped_count: NonNegativeInt
    classification: DataClassification


class DeterministicMappingResult(SensitiveMappingContract):
    """Вернуть результат SDK с версиями правил, bindings и candidates каждого поля.

    fields упорядочены по (entity_type, field_name). Fingerprints связывают
    profile/content/catalog/scope/semantic hints/options; Unicode version влияет
    на воспроизводимость names. Manifest binding сохранён в legacy MappingCandidate.
    Хеши обеспечивают согласованность snapshots, а не удостоверяют их происхождение.

    DTO immutable и не владеет I/O; неверные значения дают pydantic.ValidationError.
    Полный result чувствителен, repr скрыт. Для logs предназначен safe_summary().
    Результат не является MappingPlan, SecurityApproval или разрешением на загрузку.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    algorithm: Literal["deterministic_mapping_v1"] = "deterministic_mapping_v1"
    normalization_version: Literal["names_v1_ru_lat_v1"] = "names_v1_ru_lat_v1"
    dictionary_version: Literal["ru_en_aliases_v1"] = "ru_en_aliases_v1"
    unicode_version: IdentifierStr
    profile_fingerprint: FingerprintStr
    normalized_data_fingerprint: FingerprintStr
    options_fingerprint: FingerprintStr
    scope_fingerprint: FingerprintStr
    semantic_catalog_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    classification: DataClassification
    fields: Annotated[tuple[FieldCandidates, ...], Field(max_length=1024)]

    def field(self, entity_type: str, field_name: str) -> FieldCandidates:
        """Найти результат по точной паре имён, без нормализации и I/O.

        Args:
            entity_type: Semantic entity type профиля.
            field_name: Semantic field name внутри этого entity type.

        Returns:
            FieldCandidates с рекомендациями и explanations для указанного поля.

        Raises:
            KeyError: semantic_field_not_found без исходных имён в тексте ошибки.
        """
        for field in self.fields:
            if (field.source.entity_type, field.source.field_name) == (
                entity_type,
                field_name,
            ):
                return field
        raise KeyError("semantic_field_not_found")

    def safe_summary(self) -> MappingSafeSummary:
        """Вернуть отдельный MappingSafeSummary с counters и classification.

        Не выполняет I/O, не пишет в logger и не изменяет чувствительный полный
        result. Names/refs/hashes в summary отсутствуют; это не approval на загрузку.
        """
        return MappingSafeSummary(
            field_count=len(self.fields),
            candidate_count=sum(len(f.candidates) for f in self.fields),
            ambiguous_count=sum(f.ambiguous for f in self.fields),
            unmapped_count=sum(f.status == "unmapped" for f in self.fields),
            classification=self.classification,
        )
