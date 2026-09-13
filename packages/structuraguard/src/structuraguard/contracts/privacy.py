"""Configurable detection и безопасные metadata без raw исходных значений."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import DataClassification, UtcDateTime


class SensitiveCategory(StrEnum):
    """Закрытые категории findings; enum не включает автоматический NLP.

    PERSON_NAME зарезервирован, встроенного распознавания ФИО сейчас нет."""

    EMAIL = "email"
    PHONE = "phone"
    INN = "inn"
    SNILS = "snils"
    PASSPORT = "passport"
    CARD = "card"
    API_KEY = "api_key"
    TOKEN = "token"
    PRIVATE_KEY = "private_key"
    PASSWORD = "password"
    PERSON_NAME = "person_name"
    CUSTOM = "custom"


class ScanLimits(FrozenContract):
    """Конечные caps проверяются до materialization и regex passes."""

    max_chars: Annotated[StrictInt, Field(gt=0, le=1_000_000)] = 65_536
    max_bytes: Annotated[StrictInt, Field(gt=0, le=4_000_000)] = 262_144
    max_fields: Annotated[StrictInt, Field(gt=0, le=1024)] = 128
    max_chunks: Annotated[StrictInt, Field(gt=0, le=10000)] = 1024
    max_findings: Annotated[StrictInt, Field(gt=0, le=10000)] = 1024
    max_work: Annotated[StrictInt, Field(gt=0, le=1_000_000_000)] = 100_000_000
    max_time_ms: Annotated[StrictInt, Field(gt=0, le=300000)] = 1000
    max_output_chars: Annotated[StrictInt, Field(gt=0, le=4_000_000)] = 262_144


class CustomPattern(FrozenContract):
    """Trusted ASCII regex subset; pattern не используется как имя finding."""

    pattern: Annotated[
        str, Field(strict=True, min_length=1, max_length=256, repr=False)
    ]
    classification: DataClassification = DataClassification.RESTRICTED

    @model_validator(mode="after")
    def floor(self) -> Self:
        """Отклонить класс ниже CONFIDENTIAL через ValueError."""
        if self.classification not in {
            DataClassification.CONFIDENTIAL,
            DataClassification.RESTRICTED,
        }:
            raise ValueError("Custom findings требуют минимум CONFIDENTIAL")
        return self


class CategoryRule(FrozenContract):
    """Повышение класса категории; PII/secret floors нельзя понизить."""

    category: SensitiveCategory
    classification: DataClassification

    @model_validator(mode="after")
    def floor(self) -> Self:
        """Проверить category floor; secrets/card ниже RESTRICTED дают ValueError."""
        if self.classification not in {
            DataClassification.CONFIDENTIAL,
            DataClassification.RESTRICTED,
        }:
            raise ValueError("PII findings требуют минимум CONFIDENTIAL")
        if (
            self.category
            in {
                SensitiveCategory.API_KEY,
                SensitiveCategory.TOKEN,
                SensitiveCategory.PRIVATE_KEY,
                SensitiveCategory.PASSWORD,
                SensitiveCategory.CARD,
            }
            and self.classification is not DataClassification.RESTRICTED
        ):
            raise ValueError("Secrets и cards нельзя понижать")
        return self


class DetectionPolicy(FrozenContract):
    """Detectors обязательны; настройка меняет precision и может повышать класс."""

    baseline: DataClassification = DataClassification.INTERNAL
    limits: ScanLimits = Field(default_factory=ScanLimits)
    inn_validation: Literal["pattern", "checksum"] = "pattern"
    phone_mode: Literal["broad", "international"] = "broad"
    custom_patterns: tuple[CustomPattern, ...] = Field(
        default=(), max_length=32, repr=False
    )
    custom_secret_fields: tuple[
        Annotated[str, Field(strict=True, min_length=1, max_length=128)], ...
    ] = Field(default=(), max_length=32, repr=False)
    categories: tuple[CategoryRule, ...] = Field(default=(), max_length=12)
    redaction_mode: Literal["irreversible", "reversible"] = "irreversible"
    map_ttl_seconds: Annotated[StrictInt, Field(gt=0, le=3600)] = 300

    @model_validator(mode="after")
    def distinct(self) -> Self:
        """Отклонить повтор category override через ValueError."""
        if len({rule.category for rule in self.categories}) != len(self.categories):
            raise ValueError("Повтор category override")
        return self

    @property
    def fingerprint(self) -> str:
        """Fingerprint trusted настроек; полный DTO не предназначен для logs."""
        return canonical_sha256_value(self)


class SensitiveFinding(FrozenContract):
    """Только позиция и категория, без label, значения, raw hash или custom regex."""

    field_index: Annotated[StrictInt, Field(ge=0, le=1024)]
    part: Literal["name", "value"]
    start: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    end: Annotated[StrictInt, Field(gt=0, le=1_000_000)]
    category: SensitiveCategory
    classification: DataClassification

    @model_validator(mode="after")
    def span(self) -> Self:
        """Проверить непустой полуинтервал Unicode offsets; иначе ValueError."""
        if self.start >= self.end:
            raise ValueError("Пустой finding span")
        return self


class CategoryCount(FrozenContract):
    """Количество findings одной категории без исходных значений и location."""

    category: SensitiveCategory
    count: Annotated[StrictInt, Field(ge=0, le=10000)]


class PrivacySummary(FrozenContract):
    """Единственное представление для logs/audit: только закрытые counts/classes."""

    classification: DataClassification
    complete: Literal[True] = True
    scanned_chars: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    finding_count: Annotated[StrictInt, Field(ge=0, le=10000)]
    categories: tuple[CategoryCount, ...]


class DetectionReport(FrozenContract):
    """Полный bounded scan; ошибка/исчерпание budget не возвращает partial clean."""

    classification: DataClassification
    complete: Literal[True] = True
    scanned_chars: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    work_used: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
    findings: tuple[SensitiveFinding, ...] = Field(max_length=10000)

    def safe_summary(self) -> PrivacySummary:
        """Не включает locations, fingerprints, labels или исходные значения."""
        return PrivacySummary(
            classification=self.classification,
            scanned_chars=self.scanned_chars,
            finding_count=len(self.findings),
            categories=tuple(
                CategoryCount(
                    category=category,
                    count=sum(f.category is category for f in self.findings),
                )
                for category in SensitiveCategory
                if any(f.category is category for f in self.findings)
            ),
        )


type OpaqueUUID = Annotated[
    str,
    Field(
        strict=True,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]


class PlaceholderMapPolicy(FrozenContract):
    """Authentication principal задаёт host; пустые allowlists отказывают."""

    runs: tuple[OpaqueUUID, ...] = Field(default=(), max_length=32)
    writers: tuple[OpaqueUUID, ...] = Field(default=(), max_length=32)
    readers: tuple[OpaqueUUID, ...] = Field(default=(), max_length=32)
    max_ttl_seconds: Annotated[StrictInt, Field(gt=0, le=3600)] = 300
    max_maps: Annotated[StrictInt, Field(gt=0, le=1000)] = 100
    max_entries: Annotated[StrictInt, Field(gt=0, le=10000)] = 1024
    max_bytes: Annotated[StrictInt, Field(gt=0, le=16_000_000)] = 4_000_000


class PlaceholderHandle(FrozenContract):
    """Opaque handle не является разрешением чтения или plaintext map."""

    map_id: OpaqueUUID
    run_id: OpaqueUUID
    expires_at: UtcDateTime
