"""Неизменяемая конфигурация SQLite/PostgreSQL inspection без import-time I/O."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, SecretStr, StrictInt, field_validator, model_validator

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import IdentifierStr

_PositiveInt = Annotated[StrictInt, Field(gt=0)]


class InspectionLimits(FrozenContract):
    """Задать budgets metadata и времени; request не может их увеличить.

    Attributes:
        max_tables: Число разрешённых объектов, включая views.
        max_columns: Число колонок на объект.
        max_constraints: Предел constraints/indexes в одном metadata запросе.
        max_items: Бюджет строк reflection и отдельной фазы сборки.
        max_metadata_bytes: Бюджет metadata и сериализованного полного каталога.
        max_sql_bytes: Размер SQLite definition или фиксированного PG-запроса.
        max_text_chars: Число символов в одном текстовом metadata поле.
        timeout_seconds: Общий deadline inspection и сборки полного каталога.
        statement_timeout_seconds: Максимальное время одного statement.
        lock_timeout_seconds: Максимальное ожидание блокировки БД.
        cleanup_seconds: Отдельный предел ожидания освобождения ресурсов.

    Raises:
        pydantic.ValidationError: Не положительное/не конечное значение
            либо превышение верхней границы поля.

    Конструирование не выполняет I/O. Эти budgets не ограничивают память
    native runtime/DB server и не заменяют OS isolation.
    """

    max_tables: _PositiveInt = 256
    max_columns: _PositiveInt = 500
    max_constraints: _PositiveInt = 1024
    max_items: _PositiveInt = 20_000
    max_metadata_bytes: _PositiveInt = 8 * 1024 * 1024
    max_sql_bytes: Annotated[StrictInt, Field(gt=0, le=1_000_000_000)] = 256 * 1024
    max_text_chars: Annotated[StrictInt, Field(gt=0, le=4096)] = 4096
    timeout_seconds: Annotated[float, Field(gt=0, le=300, allow_inf_nan=False)] = 60
    statement_timeout_seconds: Annotated[
        float, Field(gt=0, le=60, allow_inf_nan=False)
    ] = 10
    lock_timeout_seconds: Annotated[float, Field(gt=0, le=5, allow_inf_nan=False)] = 2
    cleanup_seconds: Annotated[float, Field(gt=0, le=30, allow_inf_nan=False)] = 5


class SQLiteTarget(FrozenContract):
    """Доверенная привязка target ID к существующему SQLite-файлу.

    Attributes:
        path: Абсолютный путь; существование проверяется только при inspection.
        target_id: Непрозрачная привязка, назначенная владельцем конфигурации.
        include_tables: Точные имена разрешённых объектов, включая views.
        deny_tables: Запреты с приоритетом над include; ASCII case rules SQLite.
        include_schemas: Поддержан только scope ``("main",)``.
        deny_schemas: Запрет ``main`` закрывает весь scope.
        include_columns: Пока не поддержан; непустой selector отклоняет inspection.
        deny_columns: Пока не поддержан; отказ до чтения table metadata.
        limits: Неизменяемые бюджеты inspection.

    Raises:
        pydantic.ValidationError: Относительный путь, дубликаты или unsafe names.

    Создание не выполняет I/O. Пустой/unsupported scope отклоняет adapter до
    подключения. Путь скрыт в repr, но присутствует в serialization; владелец
    отвечает за файловую привязку и symlinks. Колонки нельзя безопасно отразить
    частично, поэтому column selectors не игнорируются молча.
    """

    path: Path = Field(repr=False)
    target_id: IdentifierStr
    include_tables: tuple[IdentifierStr, ...]
    deny_tables: tuple[IdentifierStr, ...] = ()
    include_schemas: tuple[IdentifierStr, ...] = ("main",)
    deny_schemas: tuple[IdentifierStr, ...] = ()
    include_columns: tuple[IdentifierStr, ...] = ()
    deny_columns: tuple[IdentifierStr, ...] = ()
    limits: InspectionLimits = Field(default_factory=InspectionLimits)

    @field_validator(
        "include_tables",
        "deny_tables",
        "include_schemas",
        "deny_schemas",
        "include_columns",
        "deny_columns",
        mode="before",
    )
    @classmethod
    def preserve_names(cls, value: object) -> object:
        if isinstance(value, (tuple, list)) and any(
            isinstance(name, str) and name != name.strip() for name in value
        ):
            raise ValueError(
                "inspection names must not require whitespace normalization"
            )
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if not self.path.is_absolute():
            raise ValueError("SQLite target path must be absolute")
        for names in (
            self.include_tables,
            self.deny_tables,
            self.include_schemas,
            self.deny_schemas,
            self.include_columns,
            self.deny_columns,
        ):
            if len(names) != len(set(names)):
                raise ValueError("inspection scope contains duplicate names")
        return self

    @property
    def policy_fingerprint(self) -> str:
        """Вернуть SHA-256 scope/limits без I/O, пути и порядка selectors.

        Результат имеет префикс ``sha256:``. Это fingerprint policy, а не схемы
        или файла; доверенную привязку target ID к пути поддерживает владелец.
        """
        return canonical_sha256_value(
            {
                "version": "sqlite-inspection-policy-v1",
                "tables": sorted(self.include_tables),
                "deny_tables": sorted(self.deny_tables),
                "schemas": sorted(self.include_schemas),
                "deny_schemas": sorted(self.deny_schemas),
                "columns": sorted(self.include_columns),
                "deny_columns": sorted(self.deny_columns),
                "limits": self.limits.canonical_json(),
            }
        )


class PostgreSQLTarget(FrozenContract):
    """Доверенный DSN отдельного inspector и точные пары (schema, table).

    Attributes:
        dsn: SecretStr с ``postgresql+asyncpg`` URL отдельного inspector;
            явные host/user/database, из query options разрешён только ``ssl``.
        target_id: Непрозрачная привязка, назначенная владельцем конфигурации.
        include_schemas: Максимальный список разрешённых schemas.
        include_tables: Точные пары ``(schema, table)``; patterns не используются.
        deny_schemas: Запреты с приоритетом над include.
        deny_tables: Запрещённые точные пары с приоритетом над include.
        include_columns: Пока не поддержан; непустой selector отклоняет inspection.
        deny_columns: Пока не поддержан; отказ до чтения table metadata.
        limits: Неизменяемые бюджеты inspection.

    Raises:
        pydantic.ValidationError: Некорректные типы, дубликаты или unsafe names.

    Создание не выполняет I/O. URL и runtime scope проверяет adapter.
    DSN исключён из repr/serialization; transport policy и отличие inspector
    от writer задаёт владелец. Metadata не дают права расширять этот scope.
    """

    dsn: SecretStr = Field(repr=False, exclude=True)
    target_id: IdentifierStr
    include_schemas: tuple[IdentifierStr, ...]
    include_tables: tuple[tuple[IdentifierStr, IdentifierStr], ...]
    deny_schemas: tuple[IdentifierStr, ...] = ()
    deny_tables: tuple[tuple[IdentifierStr, IdentifierStr], ...] = ()
    include_columns: tuple[IdentifierStr, ...] = ()
    deny_columns: tuple[IdentifierStr, ...] = ()
    limits: InspectionLimits = Field(default_factory=InspectionLimits)

    @model_validator(mode="before")
    @classmethod
    def preserve_names(cls, value: object) -> object:
        if isinstance(value, dict):
            for field in (
                "include_schemas",
                "deny_schemas",
                "include_tables",
                "deny_tables",
            ):
                entries = value.get(field, ())
                if not isinstance(entries, (list, tuple)):
                    continue
                for entry in entries:
                    parts = entry if isinstance(entry, (list, tuple)) else (entry,)
                    if any(
                        isinstance(part, str) and part != part.strip() for part in parts
                    ):
                        raise ValueError(
                            "inspection names must not require normalization"
                        )
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        for names in (
            self.include_schemas,
            self.deny_schemas,
            self.include_tables,
            self.deny_tables,
            self.include_columns,
            self.deny_columns,
        ):
            if len(names) != len(set(names)):
                raise ValueError("inspection scope contains duplicate names")
        return self

    @property
    def policy_fingerprint(self) -> str:
        """Вернуть SHA-256 scope/limits без I/O, DSN и порядка selectors.

        Результат имеет префикс ``sha256:``. Это policy fingerprint; он не
        удостоверяет endpoint, credentials или текущее содержимое схемы.
        """
        return canonical_sha256_value(
            {
                "version": "postgresql-inspection-policy-v1",
                "schemas": sorted(self.include_schemas),
                "tables": sorted(self.include_tables),
                "deny_schemas": sorted(self.deny_schemas),
                "deny_tables": sorted(self.deny_tables),
                "columns": sorted(self.include_columns),
                "deny_columns": sorted(self.deny_columns),
                "limits": self.limits.canonical_json(),
            }
        )
