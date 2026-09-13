"""Configurable DB maximum; SQL и credentials не входят в contract."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictInt, model_validator

from structuraguard.contracts._base import FrozenContract

type PolicyIdentifier = Annotated[
    str, Field(strict=True, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
]


class DatabaseTableRule(FrozenContract):
    """Точные schema/table/column selectors без SQL quoting и wildcards.

    Имена — ASCII до 63 символов. Пустой columns закрывает доступ; правила
    самостоятельно не проверяют grants и не открывают DB connection."""

    schema_name: PolicyIdentifier
    table_name: PolicyIdentifier
    columns: tuple[PolicyIdentifier, ...] = Field(default=(), max_length=1024)


class DatabasePolicy(FrozenContract):
    """Exact selectors; deny побеждает allow, пустой scope закрывает доступ.

    Inspector возвращает полный constraint-aware каталог. Неполный column scope
    отвергает таблицу до чтения definitions/comments, не скрывает constraints.
    Отдельные реальные grants дополнительно проверяют существующие adapters.
    """

    version: Literal[1] = 1
    allowed_schemas: tuple[PolicyIdentifier, ...] = Field(default=(), max_length=64)
    allowed_tables: tuple[DatabaseTableRule, ...] = Field(default=(), max_length=256)
    denied_schemas: tuple[PolicyIdentifier, ...] = Field(default=(), max_length=64)
    denied_tables: tuple[tuple[PolicyIdentifier, PolicyIdentifier], ...] = Field(
        default=(), max_length=256
    )
    denied_columns: tuple[
        tuple[PolicyIdentifier, PolicyIdentifier, PolicyIdentifier], ...
    ] = Field(default=(), max_length=4096)
    inspector_principal: PolicyIdentifier
    writer_principal: PolicyIdentifier
    require_signed_audit: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def unique_scope(self) -> Self:
        """Отклонить одинаковые principals и повтор selectors через ValueError."""
        if self.inspector_principal == self.writer_principal:
            raise ValueError("DB policy требует отдельные principals")
        names = [(r.schema_name, r.table_name) for r in self.allowed_tables]
        if len(names) != len(set(names)) or any(
            len(r.columns) != len(set(r.columns)) for r in self.allowed_tables
        ):
            raise ValueError("Повтор DB policy selector")
        return self


class PostgreSQLAuditPolicy(FrozenContract):
    """Настройки заранее установленного append-only audit в target transaction.

    schema_name — выделенная sg_staging_audit_* schema; namespace/actor_id/key_id —
    UUID trusted host. max_events — finite chain cap. DTO не читает ключи и не
    создаёт таблицы; неверные поля дают Pydantic ValidationError. Bootstrap явный."""

    schema_name: Annotated[
        str, Field(strict=True, pattern=r"^sg_staging_audit_[a-z0-9_]{1,24}$")
    ]
    namespace: UUID
    actor_id: UUID
    key_id: UUID
    max_events: Annotated[StrictInt, Field(gt=0, le=100_000)] = 10_000
