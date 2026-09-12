"""Bounded intake; неизвестные управляющие поля не становятся SQL или diagnostics."""

import json
from dataclasses import dataclass
from decimal import Context, Decimal, InvalidOperation

from pydantic import TypeAdapter
from pydantic import ValidationError as ModelError

from structuraguard.contracts.common import LoadOperation
from structuraguard.contracts.database import (
    DatabaseCatalog,
    SchemaCatalog,
    TableCatalog,
)
from structuraguard.contracts.deterministic_mapping import Score
from structuraguard.contracts.mapping import FieldMapping, MappingPlan
from structuraguard.contracts.mapping_rules import (
    MappingIdentity,
    MappingIssueLocation,
    MappingRelation,
)
from structuraguard.contracts.mapping_validation import MappingValidationOptions
from structuraguard.exceptions import MappingError

from ._inputs import bounded_size
from ._validation_report import Issues, failure


def preflight(value: object, options: MappingValidationOptions) -> None:
    try:
        bounded_size(value, options.max_input_bytes)
    except MappingError:
        raise failure("MAPPING_LIMIT_EXCEEDED") from None
    except (UnicodeError, AttributeError):
        # model_construct может оставить обязательное поле без атрибута.
        raise failure("MAPPING_PLAN_INVALID") from None


def catalog_limits(catalog: DatabaseCatalog, options: MappingValidationOptions) -> None:
    """Отсечь oversized snapshot до model_dump и повторной проверки metadata."""
    if not isinstance(catalog.schemas, tuple | list):
        raise failure("MAPPING_PLAN_INVALID")
    table_count = column_count = edge_count = 0
    for schema in catalog.schemas:
        if type(schema) is not SchemaCatalog or not isinstance(
            schema.tables, tuple | list
        ):
            raise failure("MAPPING_PLAN_INVALID")
        table_count += len(schema.tables)
        if table_count > options.max_tables:
            raise failure("MAPPING_LIMIT_EXCEEDED")
        for table in schema.tables:
            if (
                type(table) is not TableCatalog
                or not isinstance(table.columns, tuple | list)
                or not isinstance(table.foreign_keys, tuple | list)
            ):
                raise failure("MAPPING_PLAN_INVALID")
            column_count += len(table.columns)
            edge_count += len(table.foreign_keys)
            if column_count > options.max_columns or edge_count > options.max_edges:
                raise failure("MAPPING_LIMIT_EXCEEDED")


def decode(payload: bytes, options: MappingValidationOptions) -> dict[str, object]:
    if len(payload) > options.max_input_bytes:
        raise failure("MAPPING_LIMIT_EXCEEDED")
    # Ограничить nesting до json.loads, включая входы без закрывающих скобок.
    depth = 0
    quoted = escaped = False
    for char in payload:
        if quoted:
            if escaped:
                escaped = False
            elif char == 92:
                escaped = True
            elif char == 34:
                quoted = False
        elif char == 34:
            quoted = True
        elif char in (91, 123):
            depth += 1
            if depth > 64:
                raise failure("MAPPING_LIMIT_EXCEEDED")
        elif char in (93, 125):
            depth -= 1

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("non_finite_number")

    number_context = Context(traps=[InvalidOperation])

    def decimal_number(value: str) -> Decimal:
        # Повреждённый exponent не меняет flags/traps Decimal context приложения.
        return Decimal(value, context=number_context)

    try:
        value: object = json.loads(
            payload,
            object_pairs_hook=pairs,
            parse_float=decimal_number,
            parse_constant=invalid_constant,
        )
    except (ValueError, UnicodeError, RecursionError, InvalidOperation):
        raise failure("MAPPING_PLAN_INVALID") from None
    if not isinstance(value, dict):
        raise failure("MAPPING_PLAN_INVALID")
    preflight(value, options)
    return value


@dataclass
class PlanParts:
    payload: dict[str, object]
    plan: MappingPlan | None
    mappings: tuple[tuple[int, FieldMapping], ...]
    identities: tuple[tuple[int, MappingIdentity], ...]
    relations: tuple[tuple[int, MappingRelation], ...]
    operation: LoadOperation | None
    confidence: Decimal | None


def parse_parts(payload: dict[str, object], issues: Issues) -> PlanParts:
    preflight(payload, issues.options)
    for index, name in enumerate(
        sorted(set(payload) - MappingPlan.model_fields.keys())
    ):
        code = (
            "MAPPING_SQL_FORBIDDEN"
            if name in {"sql", "query", "expression", "script", "on_conflict_sql"}
            else "MAPPING_PLAN_INVALID"
        )
        if name in {
            "ddl",
            "create_table",
            "alter_table",
            "drop_table",
            "disable_constraints",
        }:
            code = "MAPPING_DDL_FORBIDDEN"
        issues.add(code, MappingIssueLocation(index=index))
    if payload.get("schema_version", "1.0.0") not in ("1.0.0", "1.1.0"):
        issues.add("MAPPING_PLAN_VERSION_UNSUPPORTED")
    operation = None
    try:
        operation = TypeAdapter(LoadOperation).validate_python(payload.get("operation"))
    except ModelError:
        issues.add("MAPPING_OPERATION_FORBIDDEN")
        raw = payload.get("operation")
        if isinstance(raw, str) and raw.strip().upper().split(" ")[0] in {
            "CREATE",
            "ALTER",
            "DROP",
            "TRUNCATE",
            "GRANT",
            "REVOKE",
        }:
            issues.add("MAPPING_DDL_FORBIDDEN")
    confidence = None
    try:
        confidence = TypeAdapter(Score).validate_python(payload.get("confidence"))
    except ModelError:
        issues.add("MAPPING_PLAN_INVALID")
    mappings: list[tuple[int, FieldMapping]] = []
    raw_mappings = payload.get("mappings")
    if isinstance(raw_mappings, list | tuple):
        if len(raw_mappings) > issues.options.max_mappings:
            raise failure("MAPPING_LIMIT_EXCEEDED")
        sources: set[object] = set()
        targets: set[object] = set()
        for index, raw_mapping in enumerate(raw_mappings):
            loc = MappingIssueLocation(section="mappings", index=index)
            try:
                mapping = FieldMapping.model_validate(raw_mapping)
                TypeAdapter(Score).validate_python(mapping.confidence)
            except ModelError:
                issues.add("MAPPING_PLAN_INVALID", loc)
                if isinstance(raw_mapping, dict) and any(
                    k in raw_mapping for k in ("sql", "expression", "query", "script")
                ):
                    issues.add("MAPPING_SQL_FORBIDDEN", loc)
                continue
            if mapping.source in sources:
                issues.add("MAPPING_SOURCE_CONFLICT", loc)
            if mapping.target in targets:
                issues.add("MAPPING_TARGET_AMBIGUOUS", loc)
            sources.add(mapping.source)
            targets.add(mapping.target)
            mappings.append((index, mapping))
    identities: list[tuple[int, MappingIdentity]] = []
    relations: list[tuple[int, MappingRelation]] = []
    for section in ("identities", "relations"):
        items = payload.get(section)
        if items is None:
            continue
        if not isinstance(items, tuple | list):
            issues.add("MAPPING_PLAN_INVALID")
            continue
        if len(items) > issues.options.max_edges:
            raise failure("MAPPING_LIMIT_EXCEEDED")
        for index, item in enumerate(items):
            try:
                if section == "identities":
                    identities.append((index, MappingIdentity.model_validate(item)))
                else:
                    relations.append((index, MappingRelation.model_validate(item)))
            except ModelError:
                issues.add(
                    "MAPPING_PLAN_INVALID",
                    MappingIssueLocation(section=section, index=index),
                )
    plan = None
    try:
        plan = MappingPlan.model_validate(payload)
    except ModelError:
        # Отличить stale content hash от прочих shape errors без подстановки SQL/данных.
        try:
            fresh = MappingPlan.model_validate(
                {k: v for k, v in payload.items() if k != "fingerprint"}
            )
        except ModelError:
            issues.add("MAPPING_PLAN_INVALID")
        else:
            if payload.get("fingerprint") != fresh.fingerprint:
                issues.add("MAPPING_PLAN_FINGERPRINT_MISMATCH")
    return PlanParts(
        payload,
        plan,
        tuple(mappings),
        tuple(identities),
        tuple(relations),
        operation,
        confidence,
    )
