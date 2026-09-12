"""Закрытые key requests и trusted policy детерминированных DB prechecks."""

from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .business_rules import BusinessRuleSet
from .common import FingerprintStr, IdentifierStr, NormalizedScalar
from .database import CatalogColumnRef
from .record_validation import RecordValidationLimits


class ConstraintTablePolicy(FrozenContract):
    """Разрешённая операция; upsert требует точной подтверждённой identity."""

    table_id: IdentifierStr
    operation: Literal["insert", "upsert"] = "insert"
    identity_column_ids: tuple[IdentifierStr, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def identity(self) -> Self:
        if (self.operation == "upsert") != bool(self.identity_column_ids) or len(
            set(self.identity_column_ids)
        ) != len(self.identity_column_ids):
            raise ValueError("Identity задаётся только для upsert")
        return self


class CheckRuleBinding(FrozenContract):
    """Trusted attestation эквивалентности CHECK; hash не доказывает эквивалентность."""

    table_id: IdentifierStr
    column_id: IdentifierStr | None = None
    expression_fingerprint: FingerprintStr
    equivalence_verified: Literal[True]
    rules: BusinessRuleSet


class ConstraintValidationPolicy(FrozenContract):
    """Владелец разрешает catalog/columns/operations; metadata не расширяют права."""

    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    tables: tuple[ConstraintTablePolicy, ...] = Field(max_length=256)
    allow_columns: tuple[CatalogColumnRef, ...] = Field(max_length=4096)
    source_identity_allow: tuple[CatalogColumnRef, ...] = Field(
        default=(), max_length=4096
    )
    checks: tuple[CheckRuleBinding, ...] = Field(default=(), max_length=512)
    limits: RecordValidationLimits = Field(default_factory=RecordValidationLimits)

    @model_validator(mode="after")
    def distinct_scope(self) -> Self:
        if (
            len({t.table_id for t in self.tables}) != len(self.tables)
            or len(set(self.allow_columns)) != len(self.allow_columns)
            or len(set(self.source_identity_allow)) != len(self.source_identity_allow)
        ):
            raise ValueError("Повторяющийся scope")
        if not set(self.source_identity_allow) <= set(self.allow_columns):
            raise ValueError("Source identity вне column allowlist")
        bindings = {
            (c.table_id, c.column_id, c.expression_fingerprint) for c in self.checks
        }
        if len(bindings) != len(self.checks) or any(
            not c.rules.rules for c in self.checks
        ):
            raise ValueError("Повторяющийся или пустой CHECK binding")
        return self


class ConstraintLookup(FrozenContract):
    """EXISTS по ordered key; identity отделяет upsert той же строки от конфликта."""

    lookup_id: IdentifierStr
    table_id: IdentifierStr
    column_ids: tuple[IdentifierStr, ...] = Field(min_length=1, max_length=32)
    values: tuple[NormalizedScalar, ...] = Field(
        min_length=1, max_length=32, repr=False
    )
    identity_column_ids: tuple[IdentifierStr, ...] = Field(default=(), max_length=32)
    identity_values: tuple[NormalizedScalar, ...] = Field(
        default=(), max_length=32, repr=False
    )

    @model_validator(mode="after")
    def arity(self) -> Self:
        if (
            len(self.column_ids) != len(self.values)
            or len(self.identity_column_ids) != len(self.identity_values)
            or len(set(self.column_ids)) != len(self.column_ids)
            or len(set(self.identity_column_ids)) != len(self.identity_column_ids)
        ):
            raise ValueError("Неверная arity ключа")
        return self


class ConstraintReadRequest(FrozenContract):
    """Все queries одного read snapshot; fingerprint связывает полный набор keys."""

    target_id: IdentifierStr
    target_policy_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    lookups: tuple[ConstraintLookup, ...] = Field(max_length=10000)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if len({k.lookup_id for k in self.lookups}) != len(self.lookups):
            raise ValueError("Повторяющийся lookup ID")
        return self

    @property
    def fingerprint(self) -> str:
        """Вычислить hash request и keys без I/O; это привязка, а не DB permission."""
        return canonical_sha256_value(self.canonical_json())


class ConstraintMatch(FrozenContract):
    """Только boolean outcomes; rows и ключи БД не публикуются."""

    lookup_id: IdentifierStr
    exists: bool
    conflicts: bool

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.conflicts and not self.exists:
            raise ValueError("Конфликт требует существующей строки")
        return self


class ConstraintReadResult(FrozenContract):
    """Request binding и fingerprint результатов одной read-only transaction."""

    request_fingerprint: FingerprintStr
    snapshot_fingerprint: FingerprintStr
    consistency: Literal["single_transaction"] = "single_transaction"
    matches: tuple[ConstraintMatch, ...] = Field(max_length=10000, repr=False)


class ConstraintReadPolicy(FrozenContract):
    """Отдельный trusted доступ к key columns и бюджеты queries/rows/time."""

    allow_columns: tuple[CatalogColumnRef, ...] = Field(max_length=4096)
    max_queries: Annotated[StrictInt, Field(ge=1, le=10000)] = 2048
    chunk_size: Annotated[StrictInt, Field(ge=1, le=64)] = 32
    limits: RecordValidationLimits = Field(default_factory=RecordValidationLimits)
