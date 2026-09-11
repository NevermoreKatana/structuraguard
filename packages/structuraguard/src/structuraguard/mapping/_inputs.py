"""Bounded preflight до serialization; policy и hashes не заменяют caller trust."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
)
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog
from structuraguard.domain.database_fingerprint import verify_database_fingerprint
from structuraguard.exceptions import MappingError


def failure(code: str, reason: str = "invalid_snapshot") -> MappingError:
    return MappingError(
        error_code=code,
        message="Детерминированное сопоставление отклонено.",
        details={"reason": reason},
    )


def bounded_size(value: object, cap: int) -> int:
    """Консервативная оценка allocations до обхода model_dump/hash."""
    stack = [(value, 0)]
    size = 0
    while stack:
        item, depth = stack.pop()
        size += 64
        if size > cap or depth > 64:
            raise failure("MAPPING_LIMIT_EXCEEDED", "input_size_or_depth")
        if isinstance(item, str | bytes):
            if len(item) > 65536:
                raise failure("MAPPING_LIMIT_EXCEEDED", "scalar_bytes")
            size += (
                len(item.encode("utf-8")) if isinstance(item, str) else len(item)
            ) * 4
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise failure("MAPPING_LIMIT_EXCEEDED", "numeric_size")
            size += item.bit_length() // 8
        elif isinstance(item, Decimal):
            if not item.is_finite() or item.__sizeof__() > 4352:
                raise failure("MAPPING_LIMIT_EXCEEDED", "numeric_size")
            parts = item.as_tuple()
            if (
                len(parts.digits) > 1024
                or not isinstance(parts.exponent, int)
                or abs(parts.exponent) > 1024
            ):
                raise failure("MAPPING_LIMIT_EXCEEDED", "numeric_size")
            size += len(parts.digits) * 4
        elif isinstance(item, BaseModel):
            names = type(item).model_fields
            if size + (len(stack) + len(names)) * 64 > cap:
                raise failure("MAPPING_LIMIT_EXCEEDED", "input_bytes")
            stack.extend((getattr(item, name), depth + 1) for name in names)
        elif isinstance(item, tuple | list | dict):
            if size + (len(stack) + len(item) * 2) * 64 > cap:
                raise failure("MAPPING_LIMIT_EXCEEDED", "input_bytes")
            if isinstance(item, dict):
                for key, val in item.items():
                    if not isinstance(key, str):
                        raise failure("MAPPING_INPUT_INVALID")
                    stack.extend(((key, depth + 1), (val, depth + 1)))
            else:
                stack.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, bool | float | date | datetime):
            raise failure("MAPPING_INPUT_INVALID")
        if size > cap:
            raise failure("MAPPING_LIMIT_EXCEEDED", "input_bytes")
    return size


@dataclass
class Budget:
    """Счётчики принадлежат одному rank call, глобального state нет."""

    options: DeterministicMappingOptions
    operations: int = 0
    state: int = 0

    def work(self, count: int = 1) -> None:
        self.operations += count
        if self.operations > self.options.max_operations:
            raise failure("MAPPING_LIMIT_EXCEEDED", "operations")

    def retain(self, size: int) -> None:
        self.state += size
        if self.state > self.options.max_state_bytes:
            raise failure("MAPPING_LIMIT_EXCEEDED", "retained_state")


def validate_inputs(
    profile: NormalizedDataProfile,
    catalog: DatabaseCatalog,
    scope: MappingScope,
    semantic: DatabaseSemanticCatalog | dict[str, object] | None,
    options: DeterministicMappingOptions,
) -> tuple[
    NormalizedDataProfile, DatabaseCatalog, MappingScope, DatabaseSemanticCatalog, int
]:
    """Ревалидировать также model_copy/model_construct без вывода payload ошибок."""
    try:
        size = bounded_size(
            (profile, catalog, scope, semantic), options.max_input_bytes
        )
        if (
            type(profile) is not NormalizedDataProfile
            or type(catalog) is not DatabaseCatalog
            or type(scope) is not MappingScope
        ):
            raise failure("MAPPING_INPUT_INVALID")
        if (
            profile.schema_version != "1.0.0"
            or catalog.schema_version != "1.1.0"
            or catalog.fingerprint_version != "catalog-v1"
            or catalog.dialect not in {"sqlite", "postgresql"}
        ):
            raise failure("MAPPING_UNSUPPORTED_SCHEMA")
        profile = NormalizedDataProfile.model_validate(
            profile.model_dump(mode="python")
        )
        catalog = DatabaseCatalog.model_validate(catalog.model_dump(mode="python"))
        scope = MappingScope.model_validate(scope.model_dump(mode="python"))
        if (scope.target_id, scope.target_policy_fingerprint) != (
            catalog.target_id,
            catalog.target_policy_fingerprint,
        ):
            raise failure("MAPPING_BINDING_MISMATCH")
        verify_database_fingerprint(catalog, catalog.database_fingerprint)
        if (
            len(profile.fields) > options.max_fields
            or len(profile.relationships) > options.max_relationships
        ):
            raise failure("MAPPING_LIMIT_EXCEEDED", "source_fields_or_relationships")
        graph = catalog.dependency_graph
        if graph is None or len(graph.edges) > options.max_edges:
            raise failure("MAPPING_LIMIT_EXCEEDED", "graph_edges")
        known = {f.field for f in profile.fields}
        for field in profile.fields:
            if any(label.field != field.field for label in field.labels):
                raise failure("MAPPING_BINDING_MISMATCH")
            if sum(k.count for k in field.observed_kinds) not in {
                field.entity_count,
                field.non_null_count,
            }:
                raise failure("MAPPING_INPUT_INVALID", "inconsistent_observations")
            if len({p.code for p in field.patterns}) != len(field.patterns) or any(
                p.count > field.pattern_checked for p in field.patterns
            ):
                raise failure("MAPPING_INPUT_INVALID", "inconsistent_patterns")
            if field.pattern_checked + field.pattern_skipped > field.non_null_count:
                raise failure("MAPPING_INPUT_INVALID", "inconsistent_coverage")
        if any(
            r.left not in known or r.right not in known for r in profile.relationships
        ):
            raise failure("MAPPING_BINDING_MISMATCH")
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise failure("MAPPING_INPUT_INVALID") from None
    try:
        if semantic is None:
            semantic = DatabaseSemanticCatalog(
                target_id=catalog.target_id,
                target_policy_fingerprint=catalog.target_policy_fingerprint,
                database_fingerprint=catalog.database_fingerprint,
            )
        else:
            semantic = DatabaseSemanticCatalog.model_validate(
                semantic.model_dump(mode="python")
                if isinstance(semantic, DatabaseSemanticCatalog)
                else semantic
            )
        if (
            semantic.target_id,
            semantic.target_policy_fingerprint,
            semantic.database_fingerprint,
        ) != (
            catalog.target_id,
            catalog.target_policy_fingerprint,
            catalog.database_fingerprint,
        ):
            raise failure("MAPPING_BINDING_MISMATCH")
        tables = {(t.schema_name, t.name): t for s in catalog.schemas for t in s.tables}
        aliases = 0
        for entry in semantic.tables:
            target = tables.get((entry.schema_name, entry.table_name))
            if target is None:
                raise failure("MAPPING_SEMANTIC_CATALOG_INVALID")
            columns = {c.name for c in target.columns}
            if any(c.column_name not in columns for c in entry.columns) or any(
                c not in columns for key in entry.identity_keys for c in key
            ):
                raise failure("MAPPING_SEMANTIC_CATALOG_INVALID")
            aliases += len(entry.aliases) + sum(len(c.aliases) for c in entry.columns)
        if aliases > options.max_aliases:
            raise failure("MAPPING_LIMIT_EXCEEDED", "aliases")
    except (ValidationError, ValueError, TypeError, AttributeError, RecursionError):
        raise failure("MAPPING_SEMANTIC_CATALOG_INVALID") from None
    return profile, catalog, scope, semantic, size
