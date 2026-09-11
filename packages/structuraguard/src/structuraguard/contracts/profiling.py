"""Ограниченные DTO профиля normalized stream; значения остаются чувствительными."""

from collections.abc import Iterator
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictStr, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import (
    DataClassification,
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    NormalizedScalar,
    PositiveInt,
    ProducerMetadata,
    SourceArtifactRef,
)
from structuraguard.contracts.normalized import SemanticFieldRef

Ratio = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
TypeKind = Literal[
    "string",
    "integer",
    "number",
    "decimal",
    "boolean",
    "date",
    "datetime",
    "money",
    "identifier",
]
PatternCode = Literal[
    "email",
    "phone",
    "uuid",
    "url",
    "date",
    "datetime",
    "money",
    "currency",
    "russian_inn_10",
    "russian_inn_12",
    "boolean",
    "integer",
    "decimal",
    "free_text",
]
ProfileReason = Literal[
    "mixed_kinds",
    "locale_ambiguity",
    "declared_type_conflict",
    "pattern_scan_skipped",
    "insufficient_evidence",
    "competing_types",
    "parse_issues",
    "context_limit",
    "pair_limit",
    "example_too_large",
    "currency_ambiguity",
    "naive_datetime",
    "empty_field",
]
PIICategory = Literal[
    "email",
    "phone",
    "personal_tax_id",
    "person_name",
    "address",
    "financial",
    "credential",
]


class _ProfileContract(FrozenContract):
    """repr не раскрывает labels, references, extrema и сохранённые examples."""

    def __repr_args__(self) -> Iterator[tuple[str | None, object]]:
        return iter(())


class LocalePolicy(StrEnum):
    """Явная грамматика чисел и дат; UNSPECIFIED сохраняет альтернативы."""

    UNSPECIFIED = "unspecified"
    RU_RU = "ru_RU"
    EN_US = "en_US"
    EN_GB = "en_GB"


class ExamplePolicy(StrEnum):
    """Raw разрешён только для явного локального сценария без PII/unknown."""

    MASKED = "masked"
    OMIT = "omit"
    LOCAL_RAW = "local_raw"


class NormalizedProfilingOptions(_ProfileContract):
    """Неизменяемые лимиты и правила одного вызова profiler.

    Attributes:
        locale: Явная грамматика чисел/дат; unspecified сохраняет альтернативы.
        examples: MASKED по умолчанию, OMIT без удержания samples, LOCAL_RAW
            только при полном scan без findings и классе PUBLIC/INTERNAL.
        seed: Детерминированный выбор occurrences; не влияет на статистики/hash.
        max_state_bytes: Консервативный общий учёт удерживаемого состояния,
            не лимит RSS процесса. Остальные max_* задают отдельные бюджеты.
        type_support: Минимальная доля поддержки типа, по умолчанию Decimal 0.95.
        type_margin: Минимальный отрыв от конкурента, по умолчанию Decimal 0.10.

    Raises:
        ValueError: Недопустимое поле, несовместимые sample budgets или threshold
            с более чем 4096 цифрами коэффициента либо модулем exponent > 4096.

    Sample budgets согласуются по произведению max_fields × examples_per_field
    × max_example_bytes, кроме OMIT. Options не читают environment/process locale.
    Исчерпание runtime бюджета после начала вызова даёт NormalizedProfilingError.
    """

    locale: LocalePolicy = LocalePolicy.UNSPECIFIED
    examples: ExamplePolicy = ExamplePolicy.MASKED
    seed: Annotated[StrictStr, Field(max_length=128)] = "normalized_samples_v1"
    max_batch_bytes: Annotated[PositiveInt, Field(le=16777216)] = 8388608
    max_batch_items: Annotated[PositiveInt, Field(le=200000)] = 100000
    max_depth: Annotated[PositiveInt, Field(le=128)] = 64
    max_scalar_bytes: Annotated[PositiveInt, Field(le=1048576)] = 65536
    max_numeric_digits: Annotated[PositiveInt, Field(le=4096)] = 1024
    max_decimal_exponent: Annotated[PositiveInt, Field(le=4096)] = 1024
    max_pattern_bytes: Annotated[PositiveInt, Field(le=16384)] = 4096
    max_batches: Annotated[PositiveInt, Field(le=100000)] = 10000
    max_manifest_bytes: Annotated[PositiveInt, Field(le=16777216)] = 8388608
    max_ids: Annotated[PositiveInt, Field(le=1000000)] = 250000
    max_id_bytes: Annotated[PositiveInt, Field(le=67108864)] = 16777216
    max_entity_types: Annotated[PositiveInt, Field(le=256)] = 64
    max_fields: Annotated[PositiveInt, Field(le=1024)] = 256
    distinct_k: Annotated[PositiveInt, Field(ge=16, le=4096)] = 1024
    examples_per_field: Annotated[NonNegativeInt, Field(le=16)] = 8
    max_example_bytes: Annotated[PositiveInt, Field(le=1024)] = 512
    max_sample_bytes: Annotated[PositiveInt, Field(le=4194304)] = 1048576
    max_labels_per_field: Annotated[PositiveInt, Field(le=8)] = 8
    max_pairs: Annotated[PositiveInt, Field(le=4096)] = 4096
    max_pair_operations: Annotated[PositiveInt, Field(le=10000000)] = 1000000
    max_state_bytes: Annotated[PositiveInt, Field(le=134217728)] = 67108864
    max_profile_bytes: Annotated[PositiveInt, Field(le=16777216)] = 4194304
    max_processing_seconds: Annotated[
        float, Field(gt=0, le=120, allow_inf_nan=False)
    ] = 30
    cleanup_seconds: Annotated[float, Field(gt=0, le=5, allow_inf_nan=False)] = 2
    min_type_observations: Annotated[PositiveInt, Field(le=1000000)] = 20
    type_support: Ratio = Decimal("0.95")
    type_margin: Ratio = Decimal("0.10")

    @model_validator(mode="after")
    def _budgets(self) -> Self:
        # as_integer_ratio() материализует 10**scale у компактного Decimal.
        # normalize() зависит от process context и может скрыть exponent underflow.
        for threshold in (self.type_support, self.type_margin):
            if threshold.__sizeof__() > 4096 * 4 + 256:
                raise ValueError("Threshold превышает numeric budget")
            parts = threshold.as_tuple()
            if (
                len(parts.digits) > 4096
                or not isinstance(parts.exponent, int)
                or abs(parts.exponent) > 4096
            ):
                raise ValueError("Threshold превышает numeric budget")
        self.seed.encode("utf-8")
        if (
            self.examples != ExamplePolicy.OMIT
            and self.max_fields * self.examples_per_field * self.max_example_bytes
            > self.max_sample_bytes
        ):
            raise ValueError("Несовместимые sample budgets")
        return self


class ProfileLabel(_ProfileContract):
    """Контекст поля с источником происхождения, без исполнения или source I/O.

    field задаёт точный SemanticFieldRef; kind описывает назначение text,
    origin различает caller_supplied и observed_location. Text непустой,
    не длиннее 256 символов и 256 UTF-8 bytes; нарушение даёт ValueError.
    Labels могут содержать PII. Binding и origin не удостоверяют их истинность.
    """

    field: SemanticFieldRef = Field(repr=False)
    kind: Literal[
        "source_name",
        "file_name",
        "sheet",
        "json_parent",
        "xml_parent",
        "html_heading",
        "pdf_section",
        "document_table",
        "path",
    ]
    text: Annotated[StrictStr, Field(min_length=1, max_length=256)]
    origin: Literal["caller_supplied", "observed_location"] = "caller_supplied"

    @model_validator(mode="after")
    def _size(self) -> Self:
        if len(self.text.encode("utf-8")) > 256:
            raise ValueError("Label превышает byte budget")
        return self


class NormalizedProfileContext(_ProfileContract):
    """Минимальный класс и контекст одного source/plan snapshot.

    data_classification по умолчанию INTERNAL; profiler может только повысить
    этот минимум. Labels требуют source_fingerprint, extraction_fingerprint
    и parse_plan_fingerprint одновременно, иначе создание DTO даёт ValueError.
    При выполнении profile() все заданные fingerprints сверяются с потоком,
    а label.field — с конечной schema; несовпадение даёт NormalizedProfilingError.
    Отсутствующие labels не восстанавливаются чтением исходного файла.
    """

    data_classification: DataClassification = DataClassification.INTERNAL
    source_fingerprint: FingerprintStr | None = None
    extraction_fingerprint: FingerprintStr | None = None
    parse_plan_fingerprint: FingerprintStr | None = None
    labels: Annotated[tuple[ProfileLabel, ...], Field(max_length=8192)] = ()

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if self.labels and (
            self.source_fingerprint is None
            or self.extraction_fingerprint is None
            or self.parse_plan_fingerprint is None
        ):
            raise ValueError("Labels требуют полного lineage")
        return self


class ProfileCount(_ProfileContract):
    """Частота элемента закрытого словаря."""

    name: Annotated[StrictStr, Field(max_length=64)]
    count: NonNegativeInt


class PatternEvidence(_ProfileContract):
    """Проверенное число совпадений, без исходного текста."""

    code: PatternCode
    count: NonNegativeInt


class TypeCandidate(_ProfileContract):
    """Ранжированная гипотеза, не разрешение преобразования."""

    kind: TypeKind
    count: NonNegativeInt
    support: Ratio


class TypeInference(_ProfileContract):
    """Ranked candidates и явная неоднозначность/недостаток evidence."""

    algorithm: Literal["type_inference_v1"] = "type_inference_v1"
    status: Literal["resolved", "ambiguous", "insufficient_evidence"]
    inferred_type: (
        Literal[
            "string",
            "integer",
            "number",
            "decimal",
            "boolean",
            "date",
            "datetime",
            "money",
            "identifier",
        ]
        | None
    ) = None
    candidates: Annotated[tuple[TypeCandidate, ...], Field(max_length=16)] = ()
    reasons: tuple[ProfileReason, ...] = ()

    @model_validator(mode="after")
    def _decision(self) -> Self:
        if (self.status == "resolved") != (self.inferred_type is not None):
            raise ValueError("Непоследовательный inference result")
        return self


class ScalarExtrema(_ProfileContract):
    """Локальные typed extrema; безопасный summary их не содержит."""

    kind: Literal[
        "string",
        "numeric",
        "integer",
        "number",
        "decimal",
        "boolean",
        "date",
        "datetime",
    ]
    minimum: NormalizedScalar
    maximum: NormalizedScalar
    count: NonNegativeInt = 0


class ProfileExample(_ProfileContract):
    """Sample occurrence: raw scalar optional и запрещён в safe summary."""

    ordinal: PositiveInt
    kind: Literal[
        "string", "integer", "number", "decimal", "boolean", "date", "datetime"
    ]
    value: NormalizedScalar | None = None
    masked: StrictBool = True

    @model_validator(mode="after")
    def _mask(self) -> Self:
        if self.masked != (self.value is None):
            raise ValueError("Некорректная маска example")
        return self


class IdentityHint(_ProfileContract):
    """Эвристика идентичности; не PK/UNIQUE/FK approval."""

    kind: Literal["identifier", "natural_key", "code"] = "identifier"
    strength: Literal[
        "candidate", "possible_identifier", "insufficient_evidence", "none"
    ] = "none"
    signals: tuple[
        Literal["name", "pattern", "integral", "unique", "no_nulls"], ...
    ] = ()


class PIIClassificationRequest(_ProfileContract):
    """Ограниченные признаки поля для PII adapter без raw samples и I/O handles.

    field/labels, patterns/value_categories и checked/skipped counts описывают
    наблюдения; minimum_classification задаёт нижнюю границу класса.
    input_fingerprint связывает все поля request: нулевой SHA-256 placeholder
    заполняется автоматически, несовпадение переданного hash даёт ValueError.
    Labels и имя поля чувствительны; полный request не предназначен для logs.
    """

    field: SemanticFieldRef
    labels: Annotated[tuple[ProfileLabel, ...], Field(max_length=8)] = ()
    patterns: Annotated[tuple[PatternEvidence, ...], Field(max_length=32)] = ()
    checked_count: NonNegativeInt
    skipped_count: NonNegativeInt
    value_categories: Annotated[tuple[PIICategory, ...], Field(max_length=7)] = ()
    minimum_classification: DataClassification
    input_fingerprint: FingerprintStr = "sha256:" + "0" * 64

    @model_validator(mode="after")
    def _fingerprint(self) -> Self:
        expected = canonical_sha256_value(
            self, exclude_top_level=frozenset({"input_fingerprint"})
        )
        if self.input_fingerprint == "sha256:" + "0" * 64:
            object.__setattr__(self, "input_fingerprint", expected)
        elif expected != self.input_fingerprint:
            raise ValueError("PII evidence fingerprint mismatch")
        return self


class PIIClassificationResult(_ProfileContract):
    """Результат классификации, связанный с field и input_fingerprint request.

    state равен detected при наличии categories, not_detected — при полном scan
    без находок, unknown — при недостаточном evidence без находок. complete
    отражает покрытие и может быть False также при detected.
    Findings требуют минимум CONFIDENTIAL, credential — RESTRICTED;
    несогласованные поля DTO дают ValueError. Profiler дополнительно проверяет
    binding и отсутствие понижения baseline. Этот DTO не является SecurityApproval.
    """

    field: SemanticFieldRef
    input_fingerprint: FingerprintStr
    algorithm: Literal["pii_rules_v1"] = "pii_rules_v1"
    state: Literal["detected", "not_detected", "unknown"]
    categories: Annotated[tuple[PIICategory, ...], Field(max_length=7)] = ()
    classification: DataClassification
    complete: StrictBool

    @model_validator(mode="after")
    def _state(self) -> Self:
        if self.categories and self.classification in (
            DataClassification.PUBLIC,
            DataClassification.INTERNAL,
        ):
            raise ValueError("PII findings требуют минимум CONFIDENTIAL")
        if (
            "credential" in self.categories
            and self.classification != DataClassification.RESTRICTED
        ):
            raise ValueError("Credentials требуют RESTRICTED")
        if len(set(self.categories)) != len(self.categories) or (
            bool(self.categories) != (self.state == "detected")
        ):
            raise ValueError("Непоследовательные PII findings")
        if not self.complete and self.state == "not_detected":
            raise ValueError("Неполный scan не доказывает отсутствие PII")
        return self


class NormalizedFieldProfile(_ProfileContract):
    """Статистики всех occurrences пары (entity_type, field_name).

    Counts учитывают каждую entity, включая повторные в одном record и пропуски
    позднего поля. null_ratio использует entity_count, unique_ratio — non_null_count;
    нулевые знаменатели дают None. unique_mode явно разделяет exact и estimated.
    Extrema разделены по совместимым scalar-семействам; inference/identity —
    признаки для дальнейшего анализа, а не разрешение преобразования или PK/FK.
    Labels, extrema и LOCAL_RAW examples могут содержать PII; safe summary
    строится на уровне NormalizedDataProfile. Исходные значения не изменяются.
    """

    field: SemanticFieldRef
    declared_semantic_type: IdentifierStr
    entity_count: NonNegativeInt
    present_count: NonNegativeInt
    missing_count: NonNegativeInt
    explicit_null_count: NonNegativeInt
    non_null_count: NonNegativeInt
    null_count: NonNegativeInt
    null_ratio: Ratio | None
    unique_count: NonNegativeInt | None
    unique_ratio: Ratio | None
    unique_mode: Literal["exact", "estimated"]
    distinct_algorithm: Literal["kmv128_v1"] = "kmv128_v1"
    distinct_k: PositiveInt
    observed_kinds: tuple[ProfileCount, ...]
    string_count: NonNegativeInt
    min_length: NonNegativeInt | None
    max_length: NonNegativeInt | None
    total_length: NonNegativeInt
    mean_length: Decimal | None
    minimum: NormalizedScalar | None
    maximum: NormalizedScalar | None
    extrema: Annotated[tuple[ScalarExtrema, ...], Field(max_length=8)] = ()
    candidate_extrema: Annotated[tuple[ScalarExtrema, ...], Field(max_length=8)] = ()
    patterns: Annotated[tuple[PatternEvidence, ...], Field(max_length=32)] = ()
    inference: TypeInference
    identity: IdentityHint
    categorical: StrictBool = False
    examples: Annotated[tuple[ProfileExample, ...], Field(max_length=16)] = ()
    sample_eligible: NonNegativeInt
    sample_skipped: NonNegativeInt
    pattern_checked: NonNegativeInt
    pattern_skipped: NonNegativeInt
    labels: Annotated[tuple[ProfileLabel, ...], Field(max_length=8)] = ()
    context_available: StrictBool = False
    reasons: tuple[ProfileReason, ...] = ()
    pii: PIIClassificationResult

    @property
    def source_names(self) -> tuple[str, ...]:
        """Вернуть text сохранённых labels kind=source_name или пустой tuple.

        Не восстанавливает имена по opaque IDs и не выполняет I/O.
        Возвращённые строки могут содержать PII.
        """
        return tuple(label.text for label in self.labels if label.kind == "source_name")

    @model_validator(mode="after")
    def _counts(self) -> Self:
        if (
            self.present_count + self.missing_count != self.entity_count
            or self.explicit_null_count + self.non_null_count != self.present_count
            or self.null_count != self.missing_count + self.explicit_null_count
        ):
            raise ValueError("Некорректные field counts")
        if self.unique_count is not None and self.unique_count > self.non_null_count:
            raise ValueError("Distinct превышает число значений")
        if self.pii.field != self.field:
            raise ValueError("PII относится к другому полю")
        return self


class FieldRelationship(_ProfileContract):
    """Ограниченный structural signal без предположения о FK."""

    left: SemanticFieldRef
    right: SemanticFieldRef
    count: NonNegativeInt
    kind: Literal["co_occurrence", "parent_child"]


class SafeProfileSummary(FrozenContract):
    """Сводка для logs: только counts и итоговый класс данных.

    Не содержит labels/имён, значений, samples, extrema, source refs и hashes.
    pii_field_count считает поля с находками, ambiguous_field_count — поля
    со статусом ambiguous. Classification не является разрешением на egress.
    """

    record_count: NonNegativeInt
    entity_count: NonNegativeInt
    value_count: NonNegativeInt
    field_count: NonNegativeInt
    pii_field_count: NonNegativeInt
    ambiguous_field_count: NonNegativeInt
    classification: DataClassification


class NormalizedDataProfile(_ProfileContract):
    """Неизменяемый результат завершённого normalized stream.

    fields содержит статистики по SemanticFieldRef, relationships — ограниченные
    структурные признаки, reasons — сокращения общего evidence. Manifest hash
    хранит исходный lineage, normalized_data_fingerprint — versioned content,
    profile_fingerprint — canonical hash DTO с options/context и classification.
    Неверный переданный profile hash или повтор field ref даёт ValueError.

    Полный DTO, включая его JSON, чувствителен даже при masked/omitted examples:
    labels и extrema могут содержать PII. Для logs используйте safe_summary().
    Hash не анонимизирует значения и не заменяет проверку источника/approval.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    producer: ProducerMetadata
    source: SourceArtifactRef
    extraction_fingerprint: FingerprintStr
    parse_plan_fingerprint: FingerprintStr
    normalized_manifest_fingerprint: FingerprintStr
    normalized_data_fingerprint: FingerprintStr
    content_algorithm: Literal["normalized_content_v1"] = "normalized_content_v1"
    options_fingerprint: FingerprintStr
    context_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr = "sha256:" + "0" * 64
    record_count: NonNegativeInt
    entity_count: NonNegativeInt
    value_count: NonNegativeInt
    fields: Annotated[tuple[NormalizedFieldProfile, ...], Field(max_length=1024)] = ()
    relationships: Annotated[tuple[FieldRelationship, ...], Field(max_length=4096)] = ()
    reasons: tuple[ProfileReason, ...] = ()
    classification: DataClassification
    retained_state_bytes: NonNegativeInt
    sample_bytes: NonNegativeInt

    @model_validator(mode="after")
    def _hash(self) -> Self:
        if len({f.field for f in self.fields}) != len(self.fields):
            raise ValueError("Повторяющееся поле профиля")
        expected = canonical_sha256_value(
            self, exclude_top_level=frozenset({"profile_fingerprint"})
        )
        if self.profile_fingerprint == "sha256:" + "0" * 64:
            object.__setattr__(self, "profile_fingerprint", expected)
        elif expected != self.profile_fingerprint:
            raise ValueError("Profile fingerprint mismatch")
        return self

    def field(self, entity_type: str, field_name: str) -> NormalizedFieldProfile:
        """Найти поле по точному имени сущности и поля.

        Args:
            entity_type: Имя semantic entity type без автоматической нормализации.
            field_name: Имя поля внутри этой type, с учётом регистра.

        Returns:
            Сохранённый неизменяемый NormalizedFieldProfile, без I/O.

        Raises:
            KeyError: Поле отсутствует; сообщение semantic_field_not_found
                не раскрывает запрошенные имена.
        """
        for field in self.fields:
            if (field.field.entity_type, field.field.field_name) == (
                entity_type,
                field_name,
            ):
                return field
        raise KeyError("semantic_field_not_found")

    def safe_summary(self) -> SafeProfileSummary:
        """Построить сводку по разрешённому набору счётчиков и classification.

        Returns:
            SafeProfileSummary без значений, имён/labels, extrema, examples,
            source refs и fingerprints. Исходный профиль не изменяется, I/O нет.

        Метод не маскирует полный DTO и не выдаёт approval. Возвращает отдельную
        сводку, которую можно сериализовать для журналирования вместо профиля.
        """
        return SafeProfileSummary(
            record_count=self.record_count,
            entity_count=self.entity_count,
            value_count=self.value_count,
            field_count=len(self.fields),
            pii_field_count=sum(bool(f.pii.categories) for f in self.fields),
            ambiguous_field_count=sum(
                f.inference.status == "ambiguous" for f in self.fields
            ),
            classification=self.classification,
        )
