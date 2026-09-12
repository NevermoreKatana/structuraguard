"""Декларации ключей, связей и evidence M11 без инфраструктурных зависимостей."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import FrozenContract
from .common import FingerprintStr, IdentifierStr, NonNegativeInt
from .normalized import SemanticFieldRef


class MappingIdentity(FrozenContract):
    """Указать вид и полный состав ключа через catalog table/column IDs.

    column_ids содержит от 1 до 64 разных ссылок. Проверка DTO не доказывает
    уникальность: M11 сопоставляет ключ с metadata и явной policy. Дескриптор
    включается только в MappingPlan 1.1.0; сам по себе он не разрешает upsert.
    """

    table_id: IdentifierStr
    kind: Literal[
        "explicit", "source_primary_key", "unique", "natural_key", "database_generated"
    ]
    column_ids: Annotated[tuple[IdentifierStr, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_columns(self) -> Self:
        if len(set(self.column_ids)) != len(self.column_ids):
            raise ValueError("identity содержит повтор column ref")
        return self


class MappingRelation(FrozenContract):
    """Связать отражённый FK с упорядоченными column IDs и semantic source refs.

    M11 проверяет полный состав FK, порядок пар и источники его компонентов.
    source_values, mapped_parent и lookup описывают способ будущего разрешения;
    текущая проверка не читает parent rows. generated_key, deferred и two_phase
    распознаются DTO, но отклоняются M11 как неподдержанные. Дескриптор требует
    MappingPlan 1.1.0 и не содержит SQL или исполняемого join expression.
    """

    foreign_key_id: IdentifierStr
    child_table_id: IdentifierStr
    parent_table_id: IdentifierStr
    child_column_ids: Annotated[
        tuple[IdentifierStr, ...], Field(min_length=1, max_length=64)
    ]
    parent_column_ids: Annotated[
        tuple[IdentifierStr, ...], Field(min_length=1, max_length=64)
    ]
    child_sources: Annotated[tuple[SemanticFieldRef, ...], Field(max_length=64)]
    parent_sources: Annotated[tuple[SemanticFieldRef, ...], Field(max_length=64)] = ()
    strategy: Literal[
        "source_values",
        "mapped_parent",
        "lookup",
        "generated_key",
        "deferred",
        "two_phase",
    ]


class MappingIssueLocation(FrozenContract):
    """Указать issue через закрытую секцию и неотрицательные index/component.

    Для mappings/identities/relations index сохраняет позицию во входном плане,
    включая непригодные элементы. Значения и пользовательские identifiers
    в location не включаются; трактовку component задаёт соответствующее правило.
    """

    section: Literal[
        "plan",
        "mappings",
        "identities",
        "relations",
        "catalog",
        "policy",
        "manifest",
        "profile",
    ] = "plan"
    index: NonNegativeInt = 0
    component: NonNegativeInt = 0


class MappingValidationEvidence(FrozenContract):
    """Связать проверку с версиями правил, policy/options и снимками данных/БД.

    writable_fingerprint учитывает разрешённость записи отдельно от schema hash.
    locations соответствует issues результата; load_order хранит порядок выбранных
    таблиц, identities — ключи, выбранные при проверке. Необязательные profile hashes
    присутствуют только при передаче профиля. Evidence чувствителен, не является
    подписью или полномочием на запись и не заменяет проверку свежего каталога.
    """

    rules_version: Literal["mapping-validation-v1"] = "mapping-validation-v1"
    policy_fingerprint: FingerprintStr
    options_fingerprint: FingerprintStr
    writable_fingerprint: FingerprintStr
    actual_database_fingerprint: FingerprintStr
    manifest_fingerprint: FingerprintStr
    catalog_target_id: IdentifierStr
    catalog_policy_fingerprint: FingerprintStr
    profile_fingerprint: FingerprintStr | None = None
    profile_content_fingerprint: FingerprintStr | None = None
    locations: tuple[MappingIssueLocation, ...] = ()
    load_order: tuple[IdentifierStr, ...] = ()
    identities: tuple[MappingIdentity, ...] = ()
