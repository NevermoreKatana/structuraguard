"""Доверенная policy и ограниченный отчёт intake MappingPlan."""

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import (
    FingerprintStr,
    IdentifierStr,
    LoadOperation,
    PositiveInt,
    ValidationDecision,
    ValidationIssue,
)
from .database import CatalogColumnRef
from .deterministic_mapping import MappingScope, Score, SensitiveMappingContract
from .mapping_rules import MappingIdentity, MappingIssueLocation


class MappingValidationOptions(FrozenContract):
    """Задать конечные лимиты одного вызова MappingPlanValidator.

    Ограничиваются bytes входа/результата, mappings, tables, columns, edges, issues
    и оценка работы. max_edges также ограничивает число identities и relations
    по отдельности. Проверка структуры предшествует полному dump/hash входов;
    размер результата проверяется перед возвратом. Это не лимит RSS или времени.
    Превышение вызывает SDK ValidationError с MAPPING_LIMIT_EXCEEDED без усечения.
    """

    max_input_bytes: Annotated[PositiveInt, Field(le=16_777_216)] = 8_388_608
    max_result_bytes: Annotated[PositiveInt, Field(le=8_388_608)] = 4_194_304
    max_mappings: Annotated[PositiveInt, Field(le=1024)] = 1024
    max_tables: Annotated[PositiveInt, Field(le=2048)] = 512
    max_columns: Annotated[PositiveInt, Field(le=10000)] = 10000
    max_edges: Annotated[PositiveInt, Field(le=20000)] = 10000
    max_issues: Annotated[PositiveInt, Field(le=50000)] = 10000
    max_operations: Annotated[PositiveInt, Field(le=10_000_000)] = 2_000_000

    @property
    def fingerprint(self) -> str:
        """Пересчитать hash версии options без I/O."""
        return canonical_sha256_value(self)


class MappingValidationPolicy(SensitiveMappingContract):
    """Сузить inspection scope; пустой allow запрещает всё, deny имеет приоритет.

    Policy передаёт доверенный код приложения. allow_schemas и пары allow_tables
    дополняют column refs в scope; deny на любом уровне имеет приоритет.
    source_identity_allow явно разрешает запись каждой source PK колонки,
    lookup_allow — ссылки на полный parent key для source_values/lookup relations.
    natural_keys требует kind natural_key и не заменяет отражённый PK/unique key.

    confidence_threshold по умолчанию 0.90, граница включительна для плана и полей.
    allowed_operations по умолчанию допускает insert_only/upsert; policy может
    сузить этот набор. Коллекции scope канонизируются для устойчивого fingerprint.
    Hash не удостоверяет автора, endpoint или grants; имена сопоставляются
    с metadata по правилам диалекта после inspection. Полная policy чувствительна.
    """

    policy_id: IdentifierStr
    scope: MappingScope
    allow_schemas: Annotated[tuple[IdentifierStr, ...], Field(max_length=2048)]
    allow_tables: Annotated[
        tuple[tuple[IdentifierStr, IdentifierStr], ...], Field(max_length=2048)
    ]
    deny_schemas: Annotated[tuple[IdentifierStr, ...], Field(max_length=2048)] = ()
    deny_tables: Annotated[
        tuple[tuple[IdentifierStr, IdentifierStr], ...], Field(max_length=2048)
    ] = ()
    source_identity_allow: Annotated[
        tuple[CatalogColumnRef, ...], Field(max_length=10000)
    ] = ()
    lookup_allow: Annotated[tuple[CatalogColumnRef, ...], Field(max_length=10000)] = ()
    natural_keys: Annotated[tuple[MappingIdentity, ...], Field(max_length=2048)] = ()
    confidence_threshold: Score = Decimal("0.90")
    allowed_operations: tuple[LoadOperation, ...] = (
        LoadOperation.INSERT_ONLY,
        LoadOperation.UPSERT,
    )

    @model_validator(mode="after")
    def canonical_scope(self) -> Self:
        for name in (
            "allow_schemas",
            "allow_tables",
            "deny_schemas",
            "deny_tables",
            "allowed_operations",
        ):
            object.__setattr__(self, name, tuple(sorted(set(getattr(self, name)))))
        for name in ("source_identity_allow", "lookup_allow"):
            object.__setattr__(
                self,
                name,
                tuple(
                    sorted(
                        set(getattr(self, name)),
                        key=lambda r: (r.table_id, r.column_id),
                    )
                ),
            )
        if any(key.kind != "natural_key" for key in self.natural_keys):
            raise ValueError("natural_keys требует kind natural_key")
        object.__setattr__(
            self,
            "natural_keys",
            tuple(
                sorted(set(self.natural_keys), key=lambda k: (k.table_id, k.column_ids))
            ),
        )
        return self

    @property
    def fingerprint(self) -> str:
        """Hash раскрытой validation policy отдельно от inspection-policy binding."""
        return canonical_sha256_value(self)


class MappingPlanInputReport(FrozenContract):
    """Вернуть REJECTED при невозможности получить корректный MappingPlan DTO.

    issues и locations соответствуют друг другу по позиции; исходный payload
    не сохраняется. payload_fingerprint необязателен и не удостоверяет автора.
    complete=False означает нечитаемый JSON; complete=True — завершённую проверку
    формы и пригодных независимых частей, а не всех semantic rules. Wrapper
    отсутствует всегда. Превышение лимита вызывает исключение вместо этого отчёта.
    """

    decision: Literal[ValidationDecision.REJECTED] = ValidationDecision.REJECTED
    complete: StrictBool
    payload_fingerprint: FingerprintStr | None = None
    issues: tuple[ValidationIssue, ...]
    locations: tuple[MappingIssueLocation, ...]

    @model_validator(mode="after")
    def consistent_issues(self) -> Self:
        if not self.issues or len(self.issues) != len(self.locations):
            raise ValueError("intake report требует issues с locations")
        if any(i.source_refs for i in self.issues):
            raise ValueError("mapping intake не содержит physical refs")
        return self
