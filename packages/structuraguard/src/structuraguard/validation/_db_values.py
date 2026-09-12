"""Сбор field issues; pure predicates общие с read-only adapter."""

from structuraguard.contracts.database import ColumnCatalog
from structuraguard.contracts.record_validation import ValidationRecord
from structuraguard.domain.constraint_values import field_codes

from ._rule_input import Collector


def validate_fields(
    row: ValidationRecord,
    columns: tuple[ColumnCatalog, ...],
    dialect: str,
    out: Collector,
) -> set[str]:
    values = {c.field_id: c.value for c in row.values}
    invalid: set[str] = set()
    for column in columns:
        out.tick()
        codes = field_codes(column, values.get(column.column_id), dialect)
        if column.column_id in values and (not column.writable or column.generated):
            codes = (*codes, "DB_COLUMN_NOT_WRITABLE")
        if codes:
            invalid.add(column.column_id)
        for code in codes:
            out.add(code, row, (column.column_id,))
    return invalid
