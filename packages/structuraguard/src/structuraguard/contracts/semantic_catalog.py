"""Локальные semantic hints, не расширяющие DB policy."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictStr, model_validator

from ._base import canonical_sha256_value
from .common import FingerprintStr, IdentifierStr, LoadOperation
from .deterministic_mapping import SensitiveMappingContract

Alias = Annotated[StrictStr, Field(min_length=1, max_length=256)]


class SemanticColumn(SensitiveMappingContract):
    """Добавить aliases и semantic type одной точно именованной колонки.

    Attributes:
        column_name: Имя колонки внутри SemanticTable; не нормализованный selector.
        aliases: Локальные ru/en labels до 256 UTF-8 bytes; дубли и порядок канонизируются.
        description: Недоверенное описание, не используется как alias или инструкция.
        semantic_type: Необязательный hint для ожидаемых value patterns.

    DTO immutable, без I/O; неверные значения дают pydantic.ValidationError.
    Совпадение alias не снимает type/policy/FK blockers. Serialization чувствительна.
    """

    column_name: IdentifierStr
    aliases: Annotated[tuple[Alias, ...], Field(max_length=10000)] = ()
    description: Annotated[StrictStr, Field(max_length=4096)] | None = None
    semantic_type: IdentifierStr | None = None

    @model_validator(mode="after")
    def _aliases(self) -> Self:
        if any(len(a.encode("utf-8")) > 256 for a in self.aliases):
            raise ValueError("Alias превышает byte budget")
        object.__setattr__(self, "aliases", tuple(sorted(set(self.aliases))))
        return self


class SemanticTable(SensitiveMappingContract):
    """Задать semantic hints таблицы по точным schema_name/table_name.

    Attributes:
        aliases: Контекстные имена таблицы; не глобальные aliases её колонок.
        columns: Scoped SemanticColumn annotations без противоречий для одной колонки.
        identity_keys: Декларации ordered column names; порядок компонент ключа значим.
        allowed_operations: Hints будущих этапов; не grants и не выбор upsert в M9.
        description: Недоверенный текст, не исполняется и не превращается в rules.

    Коллекции hints канонизируются; DTO immutable и не выполняет I/O.
    Неверные/противоречивые annotations дают pydantic.ValidationError.
    Identity hints не подтверждают уникальность данных или стратегию загрузки.
    """

    schema_name: IdentifierStr
    table_name: IdentifierStr
    aliases: Annotated[tuple[Alias, ...], Field(max_length=10000)] = ()
    description: Annotated[StrictStr, Field(max_length=4096)] | None = None
    columns: Annotated[tuple[SemanticColumn, ...], Field(max_length=10000)] = ()
    identity_keys: tuple[tuple[IdentifierStr, ...], ...] = ()
    allowed_operations: tuple[LoadOperation, ...] = ()

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if any(len(a.encode("utf-8")) > 256 for a in self.aliases):
            raise ValueError("Alias превышает byte budget")
        unique = {c.column_name: c for c in self.columns}
        if any(unique[c.column_name] != c for c in self.columns):
            raise ValueError("Conflicting column annotations")
        if any(not key or len(set(key)) != len(key) for key in self.identity_keys):
            raise ValueError("Invalid identity key")
        object.__setattr__(self, "columns", tuple(unique[k] for k in sorted(unique)))
        object.__setattr__(self, "aliases", tuple(sorted(set(self.aliases))))
        object.__setattr__(
            self, "identity_keys", tuple(sorted(set(self.identity_keys)))
        )
        object.__setattr__(
            self, "allowed_operations", tuple(sorted(set(self.allowed_operations)))
        )
        return self


class DatabaseSemanticCatalog(SensitiveMappingContract):
    """Привязать локальные semantic hints к конкретному snapshot БД.

    Attributes:
        target_id: Тот же DB target, что у DatabaseCatalog и MappingScope.
        target_policy_fingerprint: Binding к inspection policy без расширения allowlist.
        database_fingerprint: Schema hash каталога, для которого созданы annotations.
        tables: Qualified table hints; неоднозначный alias может принадлежать нескольким
            targets и должен сохранять кандидатов, а не выбирать последний entry.

    Raises:
        pydantic.ValidationError: Неверная schema или конфликтующие annotations.
            Проверка наличия targets и binding к catalog выполняется отдельно в rank.

    Side effects:
        Нет загрузки файлов YAML/JSON, сетевых запросов или credentials. DTO immutable,
        repr скрыт; явная serialization annotations чувствительна.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    tables: Annotated[tuple[SemanticTable, ...], Field(max_length=10000)] = ()

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        if self.schema_version != "1.0.0":
            raise ValueError("Unsupported semantic catalog schema")
        unique = {(t.schema_name, t.table_name): t for t in self.tables}
        if any(unique[t.schema_name, t.table_name] != t for t in self.tables):
            raise ValueError("Conflicting table annotations")
        object.__setattr__(self, "tables", tuple(unique[k] for k in sorted(unique)))
        return self

    @property
    def fingerprint(self) -> str:
        """Вернуть канонический SHA-256 hints, включая bindings и descriptions.

        Вычисляется без I/O; эквивалентные перестановки annotations дают тот же
        hash. Изменение hint меняет binding кандидата, но hash не является подписью.
        """
        return canonical_sha256_value(self)
