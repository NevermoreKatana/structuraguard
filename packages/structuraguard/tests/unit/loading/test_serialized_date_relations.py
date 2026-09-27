"""Каноническая DATE одинаково участвует в CHECK и локальных FK связях."""

from datetime import date

import pytest
from tests.fakes.loading import dry_run_case
from tests.fakes.mapping import catalog, column, table
from tests.fakes.mapping_validation import replan
from tests.unit.validation.test_db_constraints import data, policy

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.common import DateScalar, StringScalar
from structuraguard.contracts.constraint_validation import CheckRuleBinding
from structuraguard.contracts.database import ForeignKeyCatalog, TableCatalog
from structuraguard.contracts.record_validation import ValidationCell, ValidationRecord
from structuraguard.exceptions import LoadError
from structuraguard.loading.planning import _dependencies
from structuraguard.loading.projection import prepare
from structuraguard.loading.relations import verify_mapped_parents
from structuraguard.validation import DatabaseConstraintValidator


def date_tables() -> tuple[TableCatalog, TableCatalog]:
    parent = table("parents", column("day", "date"))
    child = table(
        "children",
        column("day", "date"),
        foreign_keys=(
            ForeignKeyCatalog(
                foreign_key_id="fk_day",
                column_ids=("day",),
                referenced_table_id=parent.table_id,
                referenced_column_ids=("day",),
            ),
        ),
    )
    return parent, child


@pytest.mark.parametrize("reverse", [False, True])
def test_date_dependency_matches_both_scalar_representations(reverse: bool) -> None:
    parent, child = date_tables()
    values: list[DateScalar | StringScalar] = [
        DateScalar(value=date(2026, 1, 1)),
        StringScalar(value="2026-01-01"),
    ]
    if reverse:
        values.reverse()
    rows = tuple(
        ValidationRecord(
            record_id=name,
            collection_id=target.table_id,
            values=(ValidationCell(field_id="day", value=value),),
        )
        for name, target, value in zip(
            ("parent", "child"), (parent, child), values, strict=True
        )
    )
    before = tuple(row.canonical_json() for row in rows)
    assert _dependencies(
        rows, {parent.table_id: parent, child.table_id: child}, 1000
    ) == {
        "parent": set(),
        "child": {"parent"},
    }
    assert tuple(row.canonical_json() for row in rows) == before


@pytest.mark.anyio
@pytest.mark.parametrize("valid", [False, True])
async def test_mapped_parent_accepts_only_the_same_canonical_date(valid: bool) -> None:
    parent, child = date_tables()
    db = catalog(parent, child)
    snapshot, _ = await dry_run_case(
        db,
        [
            {
                "parent_day": DateScalar(value=date(2026, 1, 1)),
                "child_day": StringScalar(
                    value="2026-01-01" if valid else "2026-01-02"
                ),
            }
        ],
        table_name="parents",
        semantic_type="unresolved",
        targets={
            "parent_day": ("parents", "day"),
            "child_day": ("children", "day"),
        },
    )
    relation = (snapshot.mapping.relations or ())[0]
    parent_ref = next(
        m.source
        for m in snapshot.mapping.mappings
        if m.source.field_name == "parent_day"
    )
    relation = relation.model_copy(
        update={"strategy": "mapped_parent", "parent_sources": (parent_ref,)}
    )
    snapshot = snapshot.model_copy(
        update={"mapping": replan(snapshot.mapping, relations=(relation,))}
    )
    prepared = await prepare(snapshot)
    before = prepared.data.canonical_json()
    if valid:
        verify_mapped_parents(prepared, 1000, catalog=db)
    else:
        with pytest.raises(LoadError, match="LOAD_FK_PARENT_MISMATCH"):
            verify_mapped_parents(prepared, 1000, catalog=db)
    assert prepared.data.canonical_json() == before


@pytest.mark.anyio
@pytest.mark.parametrize("typed", [False, True])
async def test_bound_date_check_uses_database_value_without_rewriting_input(
    typed: bool,
) -> None:
    expression = "day >= DATE '2025-01-01'"
    target = table("events", column("day", "date")).model_copy(
        update={"check_constraints": (expression,)}
    )
    db = catalog(target)
    rules = BusinessRuleSet.model_validate(
        {
            "fields": (
                {
                    "collection_id": target.table_id,
                    "field_id": "day",
                    "value_type": "date",
                },
            ),
            "rules": (
                {
                    "op": "gte",
                    "rule_id": "earliest",
                    "collection_id": target.table_id,
                    "left": {"kind": "field", "field_id": "day", "value_type": "date"},
                    "right": {
                        "kind": "literal",
                        "value": DateScalar(value=date(2025, 1, 1)),
                    },
                    "nulls": "pass",
                },
            ),
        }
    )
    binding = CheckRuleBinding(
        table_id=target.table_id,
        expression_fingerprint=canonical_sha256_value(expression),
        equivalence_verified=True,
        rules=rules,
    )
    configured = policy(db).model_copy(update={"checks": (binding,)})
    source = data(
        (
            target.table_id,
            {
                "day": DateScalar(value=date(2026, 1, 1))
                if typed
                else StringScalar(value="2026-01-01")
            },
        )
    )
    before = source.canonical_json()
    report = await DatabaseConstraintValidator(configured).validate(source, catalog=db)
    assert report.accepted, report.issues
    assert source.canonical_json() == before
