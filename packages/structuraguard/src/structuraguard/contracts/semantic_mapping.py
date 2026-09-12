"""Неизменяемые contracts M10 для предложения mappings без исполнения плана.

Создание DTO проверяет форму без I/O и может вызвать pydantic.ValidationError,
содержащую входные значения. Extra fields запрещены. Конструктор сам по себе не
подтверждает membership, policy или безопасность ответа: это делает mapper.
Полная сериализация локальных DTO чувствительна; для logs служит safe_summary
у SemanticMappingResult. Ни один contract не разрешает загрузку данных.
"""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictStr, model_validator

from .common import (
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    PipelineStatus,
    PositiveInt,
    SchemaVersionStr,
)
from .database import ForeignKeyDependency
from .deterministic_mapping import (
    CandidateExplanation,
    DeterministicMappingResult,
    FieldCandidates,
    Score,
    SensitiveMappingContract,
)
from .llm import LLMCallRecord, LLMPrompt
from .mapping import MappingCandidate

Token = Annotated[StrictStr, Field(pattern=r"^[a-z][0-9]{1,6}$", max_length=7)]
SemanticScore = Annotated[
    StrictStr, Field(pattern=r"^(?:0\.[0-9]{6}|1\.000000)$", max_length=8)
]
Reason = Literal[
    "SEMANTIC_MATCH",
    "ENTITY_CONTEXT",
    "RELATION_CONTEXT",
    "INSUFFICIENT_EVIDENCE",
    "MULTIPLE_PLAUSIBLE_TARGETS",
    "NO_MATCH",
]
SemanticAction = Literal["auto", "confirm", "reject"]
ResponseRetention = Literal["metadata_only", "validated_decision"]


class SemanticMappingOptions(SensitiveMappingContract):
    """Задать бюджеты подготовки/ответа, веса, пороги и retention M10 без I/O.

    top_k ограничивает candidates до LLM; max_entities/max_fields/max_relations
    относятся к одной группе, max_groups — к вызову. max_seconds ограничивает
    подготовку либо весь propose; общий бюджет router действует дополнительно.
    llm_weight задаёт долю сигнала модели, а penalties — штрафы SDK. Нулевой
    штраф не снимает blocker. review_threshold должен быть ниже auto_threshold,
    ambiguity_margin — больше нуля; нарушение даёт pydantic.ValidationError.
    response_retention сохраняет проверенный decision либо только его metadata;
    raw response не сохраняется ни в одном режиме. Локальные candidates остаются
    чувствительными. Все численные ограничения можно менять внутри ceilings.
    """

    top_k: Annotated[PositiveInt, Field(le=10)] = 5
    max_groups: Annotated[PositiveInt, Field(le=64)] = 32
    max_entities: Annotated[PositiveInt, Field(le=8)] = 8
    max_fields: Annotated[PositiveInt, Field(le=32)] = 32
    max_relations: Annotated[PositiveInt, Field(le=32)] = 32
    max_payload_bytes: Annotated[PositiveInt, Field(le=131072)] = 65536
    max_response_bytes: Annotated[PositiveInt, Field(le=131072)] = 65536
    max_operations: Annotated[PositiveInt, Field(le=1_000_000)] = 100_000
    max_state_bytes: Annotated[PositiveInt, Field(le=33_554_432)] = 16_777_216
    max_seconds: Annotated[PositiveInt, Field(le=300)] = 30
    llm_weight: Annotated[Score, Field(le=Decimal("0.30"))] = Decimal("0.20")
    auto_threshold: Score = Decimal("0.90")
    review_threshold: Score = Decimal("0.70")
    ambiguity_margin: Score = Decimal("0.10")
    ambiguity_penalty: Score = Decimal("0.10")
    validation_penalty: Score = Decimal("0.10")
    security_penalty: Score = Decimal("0.20")
    response_retention: ResponseRetention = "validated_decision"

    @model_validator(mode="after")
    def _thresholds(self) -> Self:
        if self.review_threshold >= self.auto_threshold or not self.ambiguity_margin:
            raise ValueError("Некорректные пороги semantic mapping")
        return self


class SemanticMappingContext(SensitiveMappingContract):
    """Передать доверенный run_id router и минимальные классы данных/metadata.

    data_classification и metadata_classification задают нижнюю границу вместе
    с классификацией профиля. Неизвестная metadata по умолчанию RESTRICTED;
    маскирование не понижает класс. DTO не читает окружение и не проверяет grants.
    Некорректные поля отклоняются с pydantic.ValidationError при создании.
    """

    run_id: IdentifierStr
    data_classification: DataClassification = DataClassification.INTERNAL
    metadata_classification: DataClassification = DataClassification.RESTRICTED


class SemanticAssessment(SensitiveMappingContract):
    """Оценка candidate_id моделью: semantic_score и закрытый reason_code.

    Score — строка 0.000000–1.000000, один сигнал для SDK. Конструктор проверяет
    форму; принадлежность candidate проверяет mapper. Итоговый confidence не задан.
    """

    candidate_id: Token
    semantic_score: SemanticScore
    reason_code: Reason


class SemanticChoice(SensitiveMappingContract):
    """Выбор колонки или связи для source_id с полными assessments и причиной.

    selected требует selected_candidate_id; ambiguous/unmapped требуют None.
    Несогласованность даёт pydantic.ValidationError. Membership и полноту
    assessments проверяет mapper; DTO не исполняет действия модели.
    """

    source_id: Token
    status: Literal["selected", "ambiguous", "unmapped"]
    selected_candidate_id: Token | None
    assessments: Annotated[tuple[SemanticAssessment, ...], Field(max_length=10)]
    reason_code: Reason

    @model_validator(mode="after")
    def _selection(self) -> Self:
        if (self.status == "selected") != (self.selected_candidate_id is not None):
            raise ValueError("Статус не соответствует выбору")
        return self


class SemanticTableChoice(SensitiveMappingContract):
    """Выбор таблиц для source_id с assessments, status и закрытой причиной.

    selected требует непустые уникальные selected_candidate_ids, остальные
    статусы — пустой набор; иначе pydantic.ValidationError. Несколько таблиц
    означают proposal split: его FK и покрытие полями проверяет mapper.
    """

    source_id: Token
    status: Literal["selected", "ambiguous", "unmapped"]
    selected_candidate_ids: Annotated[tuple[Token, ...], Field(max_length=8)]
    assessments: Annotated[tuple[SemanticAssessment, ...], Field(max_length=10)]
    reason_code: Reason

    @model_validator(mode="after")
    def _selection(self) -> Self:
        if (self.status == "selected") != bool(self.selected_candidate_ids):
            raise ValueError("Статус не соответствует выбору таблиц")
        if len(set(self.selected_candidate_ids)) != len(self.selected_candidate_ids):
            raise ValueError("Повторяющийся выбор таблицы")
        return self


class SemanticMappingDecision(SensitiveMappingContract):
    """Закрытый ответ модели 1.0.0, привязанный к group_id и fingerprint candidates.

    tables/columns/relations содержат выборы, review_required сохраняет запрос
    review. Все поля обязательны; SQL, код, значения и свободный reasoning
    запрещены. Создание DTO не заменяет проверку membership и FK в mapper.
    """

    schema_version: Literal["1.0.0"]
    group_id: Token
    candidate_set_fingerprint: FingerprintStr
    tables: Annotated[tuple[SemanticTableChoice, ...], Field(max_length=8)]
    columns: Annotated[tuple[SemanticChoice, ...], Field(max_length=32)]
    relations: Annotated[tuple[SemanticChoice, ...], Field(max_length=32)]
    review_required: StrictBool


class SemanticFieldCandidates(SensitiveMappingContract):
    """Связать source_id поля и entity_id с исходным FieldCandidates M9.

    ranked сохраняет lineage и evidence, включая скрытую конкуренцию до top-k.
    Это локальный чувствительный lookup; полный DTO не является prompt payload.
    """

    source_id: Token
    entity_id: Token
    ranked: FieldCandidates


class SemanticTableCandidate(SensitiveMappingContract):
    """Кандидат таблицы из column top-k для entity source_id.

    candidate_id адресует локальный table_id; base_score вычислен SDK как среднее
    лучших column base scores по всем полям сущности. Ref не разрешает DB writes.
    """

    candidate_id: Token
    source_id: Token
    table_id: IdentifierStr
    base_score: Score


class SemanticColumnCandidate(SensitiveMappingContract):
    """Связать candidate_id колонки с source_id поля и table_candidate_id.

    mapping содержит точный M9 target ref, explanation — детерминированные
    сигналы и ограничения. DTO чувствителен; модель получает отдельную проекцию.
    """

    candidate_id: Token
    source_id: Token
    table_candidate_id: Token
    mapping: MappingCandidate
    explanation: CandidateExplanation


class SemanticRelationPair(SensitiveMappingContract):
    """Допустимые anchors одного компонента ordered FK из column candidate IDs.

    parent_candidates и child_candidates перечисляют стороны, allowed_pairs —
    разрешённые сочетания. DTO не проверяет значения или наличие DB parent rows.
    """

    parent_candidates: tuple[Token, ...]
    child_candidates: tuple[Token, ...]
    allowed_pairs: tuple[tuple[Token, Token], ...]


class SemanticRelationCandidate(SensitiveMappingContract):
    """Полный FK candidate с entity/table aliases, ordered pairs и base_score SDK.

    foreign_key сохраняет исходное M7 evidence, pairs — все компоненты FK.
    requires_strategy оставляет review для циклов/самоссылок; стратегия исполнения
    не создаётся. Идентификаторы и SQL от модели здесь не принимаются.
    """

    candidate_id: Token
    source_id: Token
    parent_entity_id: Token
    child_entity_id: Token
    parent_table_candidate_id: Token
    child_table_candidate_id: Token
    foreign_key: ForeignKeyDependency
    pairs: tuple[SemanticRelationPair, ...]
    base_score: Score
    requires_strategy: StrictBool


class SemanticMappingCandidateSet(SensitiveMappingContract):
    """Ограниченный lookup одной группы с fields/tables/columns/relations.

    entity_types и reasons сохраняют исходный контекст; relation_sources —
    обязательные relation contexts. payload_json — отдельная masked проекция,
    fingerprint привязывает ответ к ней. Весь DTO чувствителен и не отправляется
    provider целиком. Само его создание не заменяет SecurityScanner.
    """

    group_id: Token
    entity_types: tuple[IdentifierStr, ...]
    fields: tuple[SemanticFieldCandidates, ...]
    tables: tuple[SemanticTableCandidate, ...]
    columns: tuple[SemanticColumnCandidate, ...]
    relations: tuple[SemanticRelationCandidate, ...]
    relation_sources: tuple[Token, ...]
    reasons: tuple[IdentifierStr, ...]
    payload_json: StrictStr
    fingerprint: FingerprintStr


class SemanticMappingPreparation(SensitiveMappingContract):
    """Результат локальной подготовки: deterministic M9, groups и classification.

    Получается без provider, scanner и сетевых запросов. Это чувствительный
    промежуточный результат; classification ещё учитывает только profile evidence,
    а context mapper может её повысить. Подготовка не разрешает egress или load.
    """

    deterministic: DeterministicMappingResult
    groups: tuple[SemanticMappingCandidateSet, ...]
    classification: DataClassification


class SemanticCandidateScore(SensitiveMappingContract):
    """Разложение SDK score кандидата на deterministic/LLM signals и штрафы.

    score = clamp((1 − llm_weight) × deterministic_score + llm_weight × llm_score
    − ambiguity_penalty − validation_penalty − security_penalty, 0, 1).
    Значения вычисляет mapper; DTO только валидирует форму. Это эвристическая
    оценка, не калиброванная вероятность и не разрешение обходить blockers.
    """

    candidate_id: Token
    deterministic_score: Score
    llm_score: Score
    llm_weight: Score
    ambiguity_penalty: Score
    validation_penalty: Score = Decimal(0)
    security_penalty: Score = Decimal(0)
    score: Score


class SemanticScoredChoice(SensitiveMappingContract):
    """Решение source_id после проверки mapper и агрегации scores.

    selected_candidate_ids разрешаются только в своей группе; ambiguous/reasons
    сохраняют неопределённость и blockers. action auto/confirm/reject относится
    к принятию proposal. Даже auto не является разрешением на загрузку.
    """

    source_id: Token
    selected_candidate_ids: tuple[Token, ...]
    scores: tuple[SemanticCandidateScore, ...]
    ambiguous: StrictBool
    reasons: tuple[IdentifierStr, ...]
    action: SemanticAction = "confirm"


class SemanticGroupResult(SensitiveMappingContract):
    """Предложение одной группы с candidates, choices, confidence и metadata.

    decision отсутствует при metadata_only, no_llm или пустом наборе targets;
    raw response не хранится. confidence — минимум обязательных choices,
    action — самый строгий исход. prompt/calls и security_report_fingerprint
    сохраняют происхождение решения. Полный DTO чувствителен и не исполняется.
    """

    candidates: SemanticMappingCandidateSet
    decision: SemanticMappingDecision | None
    choices: tuple[SemanticScoredChoice, ...]
    confidence: Score
    ambiguous: StrictBool
    reasons: tuple[IdentifierStr, ...]
    prompt: LLMPrompt
    calls: tuple[LLMCallRecord, ...]
    action: SemanticAction = "confirm"
    security_report_fingerprint: FingerprintStr | None = None


class SemanticMappingResult(SensitiveMappingContract):
    """Вернуть deterministic candidates, группы и общий исход semantic proposal.

    status COMPLETED возможен только для auto всех групп; confirm/reject дают
    NEEDS_REVIEW. classification и policy/options fingerprints сохраняют контекст
    проверки, response_retention — режим хранения decision. Полная сериализация
    содержит чувствительные refs/evidence даже при metadata_only; для logs
    используйте safe_summary(). DTO не создаёт MappingPlan и не меняет БД.

    response_schema_id/version/fingerprint описывают ожидаемую schema M6 и
    сохраняются mapper во всех режимах retention. У старых результатов все три
    поля отсутствуют (None); текущий hash им не приписывается. Частичный набор
    provenance отклоняется с pydantic.ValidationError.
    """

    deterministic: DeterministicMappingResult
    groups: tuple[SemanticGroupResult, ...]
    status: Literal[PipelineStatus.COMPLETED, PipelineStatus.NEEDS_REVIEW]
    classification: DataClassification
    routing_policy_fingerprint: FingerprintStr
    options_fingerprint: FingerprintStr
    action: SemanticAction = "confirm"
    response_retention: ResponseRetention = "validated_decision"
    response_schema_id: IdentifierStr | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    response_schema_version: SchemaVersionStr | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    response_schema_fingerprint: FingerprintStr | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _schema_provenance(self) -> Self:
        present = sum(
            value is not None
            for value in (
                self.response_schema_id,
                self.response_schema_version,
                self.response_schema_fingerprint,
            )
        )
        if present not in {0, 3}:
            raise ValueError(
                "Response schema provenance требует ID, version и fingerprint"
            )
        return self

    def safe_summary(self) -> dict[str, int | str]:
        """Вернуть новый словарь для logs без I/O и изменения результата.

        Без параметров. Ключи: groups и ambiguous (числа групп), classification,
        status и action. Имена, refs, payload, hashes и значения исключены.
        Для корректного DTO исключения не ожидаются.
        """
        return {
            "groups": len(self.groups),
            "ambiguous": sum(g.ambiguous for g in self.groups),
            "classification": self.classification.value,
            "status": self.status.value,
            "action": self.action,
        }
