"""Полная pure-проверка табличного плана до публикации преобразованных строк."""

from collections import defaultdict
from decimal import Decimal

from pydantic import TypeAdapter, ValidationError

from structuraguard.contracts.database import (
    CatalogColumnRef,
    ColumnCatalog,
    DatabaseCatalog,
    TableCatalog,
)
from structuraguard.contracts.deterministic_mapping import MappingScope, Score
from structuraguard.contracts.tabular_import import (
    TabularImportAssignment,
    TabularImportLineage,
    TabularImportPlan,
    TabularImportPreview,
    TabularImportResult,
    TabularImportSource,
    TabularImportSuggestion,
)
from structuraguard.domain.database_fingerprint import verify_database_fingerprint
from structuraguard.exceptions import (
    ErrorDetailInput,
    MappingError,
    StructuraGuardError,
)

from ._validation_scope import required


class TabularImportError(MappingError):
    """Отказ табличного плана; details содержат только координаты и счётчики."""


def _failure(
    code: str,
    *,
    source_id: str | None = None,
    target: CatalogColumnRef | None = None,
    row_index: int | None = None,
    expected_parts: int | None = None,
    actual_parts: int | None = None,
    actual_confidence: Decimal | None = None,
    min_confidence: Decimal | None = None,
    actual: dict[str, ErrorDetailInput] | None = None,
    expected: dict[str, ErrorDetailInput] | None = None,
) -> TabularImportError:
    return TabularImportError(
        error_code=code,
        message="Табличный план требует проверки; данные не преобразованы.",
        details={
            "source_id": source_id,
            "target_table_id": target.table_id if target else None,
            "target_column_id": target.column_id if target else None,
            "row_index": row_index,
            "expected_parts": expected_parts,
            "actual_parts": actual_parts,
            "actual_confidence": str(actual_confidence)
            if actual_confidence is not None
            else None,
            "min_confidence": str(min_confidence)
            if min_confidence is not None
            else None,
            "actual": actual,
            "expected": expected,
        },
    )


def _operation_parameters(item: TabularImportAssignment) -> dict[str, ErrorDetailInput]:
    """Закрытые параметры допустимы в диагностике; содержимое delimiter скрыто."""
    return {
        "operation": item.operation,
        "split_mode": item.split_mode,
        "delimiter_supplied": item.delimiter is not None,
        "part_index": item.part_index,
        "part_count": item.part_count,
    }


def _catalog(
    catalog: DatabaseCatalog, scope: MappingScope
) -> tuple[TableCatalog, dict[CatalogColumnRef, ColumnCatalog]]:
    try:
        catalog = DatabaseCatalog.model_validate(catalog.model_dump())
        scope = MappingScope.model_validate(scope.model_dump())
        if len(catalog.model_dump_json().encode()) > 4_194_304:
            raise _failure("TABULAR_IMPORT_LIMIT_EXCEEDED")
        verify_database_fingerprint(catalog, catalog.database_fingerprint)
    except (ValidationError, StructuraGuardError):
        raise _failure("TABULAR_IMPORT_CATALOG_INVALID") from None
    if (
        scope.target_id != catalog.target_id
        or scope.target_policy_fingerprint != catalog.target_policy_fingerprint
    ):
        raise _failure("TABULAR_IMPORT_SCOPE_MISMATCH")
    known = {
        CatalogColumnRef(table_id=t.table_id, column_id=c.column_id): (t, c)
        for schema in catalog.schemas
        for t in schema.tables
        for c in t.columns
    }
    if not set(scope.allow) <= known.keys() or not set(scope.deny) <= known.keys():
        raise _failure("TABULAR_IMPORT_SCOPE_MISMATCH")
    allowed = set(scope.allow) - set(scope.deny)
    table_ids = {ref.table_id for ref in allowed}
    if len(table_ids) != 1 or not allowed or len(allowed) > 128:
        raise _failure("TABULAR_IMPORT_SINGLE_TABLE_REQUIRED")
    table = known[next(iter(allowed))][0]
    if (
        not table.writable
        or table.inspection is None
        or table.inspection.kind != "table"
        or table.schema_name == "information_schema"
        or table.schema_name.startswith("pg_")
        or (catalog.dialect == "sqlite" and table.name.casefold().startswith("sqlite_"))
    ):
        raise _failure("TABULAR_IMPORT_TARGET_NOT_WRITABLE")
    return table, {
        ref: known[ref][1]
        for ref in allowed
        if known[ref][1].writable and not known[ref][1].generated
    }


def tabular_import_targets(
    catalog: DatabaseCatalog, scope: MappingScope
) -> tuple[CatalogColumnRef, ...]:
    """Порядок aliases c0…cN; только разрешённые изменяемые колонки одной таблицы."""
    _, columns = _catalog(catalog, scope)
    return tuple(sorted(columns, key=lambda r: (r.table_id, r.column_id)))


def _threshold(value: Decimal) -> Decimal:
    try:
        return TypeAdapter(Score).validate_python(value)
    except ValidationError:
        raise _failure("TABULAR_IMPORT_THRESHOLD_INVALID") from None


def bind_tabular_import_plan(
    source: TabularImportSource,
    catalog: DatabaseCatalog,
    scope: MappingScope,
    suggestion: TabularImportSuggestion,
    *,
    min_confidence: Decimal = Decimal("0.85"),
) -> TabularImportPlan:
    """Разрешить opaque aliases и проверить весь snapshot без SQL и I/O."""
    try:
        source = TabularImportSource.model_validate(source.model_dump())
        suggestion = TabularImportSuggestion.model_validate(suggestion.model_dump())
    except ValidationError:
        raise _failure("TABULAR_IMPORT_INPUT_INVALID") from None
    if suggestion.decision != "map" or suggestion.reason in {
        "ambiguous",
        "unsupported",
    }:
        raise _failure("TABULAR_IMPORT_NEEDS_REVIEW")
    aliases = {
        f"c{i}": ref for i, ref in enumerate(tabular_import_targets(catalog, scope))
    }
    assignments = []
    for choice in suggestion.assignments:
        target = aliases.get(choice.target_id)
        if target is None:
            raise _failure("TABULAR_IMPORT_TARGET_UNKNOWN", source_id=choice.source_id)
        assignments.append(
            TabularImportAssignment(
                **choice.model_dump(exclude={"target_id"}),
                target=target,
            )
        )
    if not assignments:
        raise _failure("TABULAR_IMPORT_SOURCE_COVERAGE")
    plan = TabularImportPlan(
        input_fingerprint=source.fingerprint,
        database_fingerprint=catalog.database_fingerprint,
        target_id=catalog.target_id,
        target_policy_fingerprint=catalog.target_policy_fingerprint,
        scope_fingerprint=scope.fingerprint,
        assignments=tuple(assignments),
        confidence=suggestion.confidence,
    )
    execute_tabular_import(
        source.labels, source.rows, catalog, scope, plan, min_confidence=min_confidence
    )
    return plan


def _assignments(
    source: TabularImportSource,
    plan: TabularImportPlan,
    table: TableCatalog,
    columns: dict[CatalogColumnRef, ColumnCatalog],
    threshold: Decimal,
) -> dict[str, list[TabularImportAssignment]]:
    source_ids = {f"s{i}" for i in range(len(source.labels))}
    grouped: dict[str, list[TabularImportAssignment]] = defaultdict(list)
    targets: set[CatalogColumnRef] = set()
    target_names: set[str] = set()
    for item in plan.assignments:
        if item.source_id not in source_ids:
            raise _failure("TABULAR_IMPORT_SOURCE_UNKNOWN", source_id=item.source_id)
        if item.target not in columns:
            raise _failure(
                "TABULAR_IMPORT_TARGET_DENIED",
                source_id=item.source_id,
                target=item.target,
            )
        if item.target in targets or columns[item.target].name in target_names:
            raise _failure(
                "TABULAR_IMPORT_TARGET_COLLISION",
                source_id=item.source_id,
                target=item.target,
            )
        if item.confidence < threshold:
            raise _failure(
                "TABULAR_IMPORT_CONFIDENCE_LOW",
                source_id=item.source_id,
                target=item.target,
                actual_confidence=item.confidence,
                min_confidence=threshold,
            )
        if item.reason in {"ambiguous", "unsupported"}:
            raise _failure(
                "TABULAR_IMPORT_NEEDS_REVIEW",
                source_id=item.source_id,
                target=item.target,
            )
        targets.add(item.target)
        target_names.add(columns[item.target].name)
        grouped[item.source_id].append(item)
    if plan.confidence < threshold:
        raise _failure(
            "TABULAR_IMPORT_CONFIDENCE_LOW",
            actual_confidence=plan.confidence,
            min_confidence=threshold,
        )
    if set(grouped) != source_ids:
        missing = min(source_ids - grouped.keys())
        raise _failure("TABULAR_IMPORT_SOURCE_COVERAGE", source_id=missing)
    for column in table.columns:
        target = CatalogColumnRef(table_id=table.table_id, column_id=column.column_id)
        if required(column) and target not in targets:
            raise _failure("TABULAR_IMPORT_REQUIRED_TARGET_MISSING", target=target)
    for source_id, items in grouped.items():
        first = items[0]
        if first.operation == "copy":
            if len(items) != 1 or any(
                v is not None
                for v in (
                    first.split_mode,
                    first.delimiter,
                    first.part_index,
                    first.part_count,
                )
            ):
                raise _failure(
                    "TABULAR_IMPORT_OPERATION_INVALID",
                    source_id=source_id,
                    target=first.target,
                    actual={
                        **_operation_parameters(first),
                        "assignment_count": len(items),
                    },
                    expected={
                        "operation": "copy",
                        "split_mode": None,
                        "delimiter_supplied": False,
                        "part_index": None,
                        "part_count": None,
                        "assignment_count": 1,
                    },
                )
            continue
        for item in items:
            if item.operation != "split":
                continue
            if (
                item.part_count is None
                or item.part_count < 2
                or item.part_index is None
                or item.part_index >= item.part_count
                or item.split_mode is None
                or (item.split_mode == "whitespace" and item.delimiter is not None)
                or (item.split_mode == "literal" and item.delimiter is None)
            ):
                raise _failure(
                    "TABULAR_IMPORT_OPERATION_INVALID",
                    source_id=source_id,
                    target=item.target,
                    actual=_operation_parameters(item),
                    expected={
                        "operation": "split",
                        "split_mode": item.split_mode or "whitespace|literal",
                        "delimiter_supplied": item.split_mode == "literal",
                        "part_count_min": 2,
                        "part_count_max": 8,
                        "part_index_min": 0,
                        "part_index_max": (item.part_count or 8) - 1,
                    },
                )
        assert first.part_count is not None
        signature = (first.split_mode, first.delimiter, first.part_count)
        if (
            any(
                item.operation != "split"
                or (item.split_mode, item.delimiter, item.part_count) != signature
                for item in items
            )
            or len(items) != first.part_count
            or {item.part_index for item in items} != set(range(first.part_count))
        ):
            raise _failure(
                "TABULAR_IMPORT_SPLIT_COVERAGE",
                source_id=source_id,
                expected_parts=first.part_count,
                actual_parts=len(items),
                actual={
                    "assignment_count": len(items),
                    "part_indices": tuple(item.part_index for item in items),
                    "assignments": tuple(
                        _operation_parameters(item) for item in items[:8]
                    ),
                },
                expected={
                    "operation": "split",
                    "split_mode": first.split_mode,
                    "part_count": first.part_count,
                    "assignment_count": first.part_count,
                    "part_indices": tuple(range(first.part_count))
                    if first.part_count
                    else (),
                },
            )
    return grouped


def execute_tabular_import(
    labels: tuple[str, ...],
    rows: tuple[dict[str, str | None], ...],
    catalog: DatabaseCatalog,
    scope: MappingScope,
    plan: TabularImportPlan,
    *,
    min_confidence: Decimal = Decimal("0.85"),
) -> TabularImportResult:
    """Проверить все строки атомарно; выход требует штатного ingest/validation.

    Ошибки содержат 1-based row_index и opaque координаты, но не значения.
    В preview и rows остаются локальные чувствительные данные для приложения.
    """
    threshold = _threshold(min_confidence)
    try:
        source = TabularImportSource(labels=labels, rows=rows)
        plan = TabularImportPlan.model_validate(plan.model_dump())
    except ValidationError:
        raise _failure("TABULAR_IMPORT_INPUT_INVALID") from None
    table, columns = _catalog(catalog, scope)
    if (
        plan.input_fingerprint != source.fingerprint
        or plan.database_fingerprint != catalog.database_fingerprint
        or plan.target_id != catalog.target_id
        or plan.target_policy_fingerprint != catalog.target_policy_fingerprint
        or plan.scope_fingerprint != scope.fingerprint
    ):
        raise _failure("TABULAR_IMPORT_BINDING_MISMATCH")
    grouped = _assignments(source, plan, table, columns, threshold)
    names = {f"s{i}": label for i, label in enumerate(source.labels)}
    result: list[dict[str, str | None]] = []
    for row_index, row in enumerate(source.rows, 1):
        output: dict[str, str | None] = {}
        for source_id, items in grouped.items():
            raw = row[names[source_id]]
            first = items[0]
            if first.operation == "copy":
                parts = [raw]
            elif raw is None:
                assert first.part_count is not None
                parts = [None] * first.part_count
            else:
                parts = list(
                    raw.split()
                    if first.split_mode == "whitespace"
                    else raw.split(first.delimiter)
                )
                if len(parts) != first.part_count or any(part == "" for part in parts):
                    raise _failure(
                        "TABULAR_IMPORT_SPLIT_PART_COUNT",
                        source_id=source_id,
                        row_index=row_index,
                        expected_parts=first.part_count,
                        actual_parts=len(parts),
                    )
            for item in items:
                index = item.part_index if item.part_index is not None else 0
                value = parts[index]
                column = columns[item.target]
                if value is None and not column.nullable:
                    raise _failure(
                        "TABULAR_IMPORT_NULL_NOT_ALLOWED",
                        source_id=source_id,
                        target=item.target,
                        row_index=row_index,
                    )
                output[column.name] = value
        result.append(output)
    lineage = tuple(
        TabularImportLineage(
            source_id=item.source_id,
            source_name=names[item.source_id],
            target=item.target,
            target_name=columns[item.target].name,
            operation=item.operation,
            part_index=item.part_index,
            part_count=item.part_count,
            confidence=item.confidence,
            reason=item.reason,
        )
        for item in plan.assignments
    )
    return TabularImportResult(
        input_fingerprint=source.fingerprint,
        plan_fingerprint=plan.fingerprint,
        table_id=table.table_id,
        rows=tuple(result),
        lineage=lineage,
        confidence=min(plan.confidence, *(a.confidence for a in plan.assignments)),
        preview=tuple(
            TabularImportPreview(
                row_index=i + 1, before=dict(before), after=dict(after)
            )
            for i, (before, after) in enumerate(
                zip(source.rows[:3], result[:3], strict=True)
            )
        ),
    )
