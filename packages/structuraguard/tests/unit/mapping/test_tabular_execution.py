"""Семантические aliases и разделение значений до стандартного ingest."""

from collections.abc import Mapping
from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, scope_for, table

from structuraguard.contracts.database import DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import MappingScope
from structuraguard.contracts.tabular_import import (
    TabularCopyChoice,
    TabularImportChoice,
    TabularImportPlan,
    TabularImportSource,
    TabularImportSuggestion,
    TabularSplitChoice,
)
from structuraguard.mapping.tabular_execution import (
    TabularImportError,
    bind_tabular_import_plan,
    execute_tabular_import,
    tabular_import_targets,
)


def sample() -> tuple[
    TabularImportSource, DatabaseCatalog, MappingScope, TabularImportSuggestion
]:
    source = TabularImportSource(
        labels=("Индекс", "Фамилия_имя"),
        rows=(
            {"Индекс": "666333", "Фамилия_имя": "Иванов Иван"},
            {"Индекс": "001000", "Фамилия_имя": "Петров Пётр"},
        ),
    )
    db = catalog(
        table("contacts", column("postal_code"), column("name"), column("second_name"))
    )
    scope = scope_for(db)
    aliases = {
        ref.column_id: f"c{i}"
        for i, ref in enumerate(tabular_import_targets(db, scope))
    }
    choices: tuple[TabularImportChoice, ...] = (
        TabularCopyChoice(
            source_id="s0",
            target_id=aliases["postal_code"],
            operation="copy",
            split_mode=None,
            delimiter=None,
            part_index=None,
            part_count=None,
            confidence=Decimal("0.99"),
            reason="semantic_name",
        ),
        *(
            TabularSplitChoice(
                source_id="s1",
                target_id=aliases[name],
                operation="split",
                split_mode="whitespace",
                delimiter=None,
                part_index=i,
                part_count=2,
                confidence=Decimal("0.99"),
                reason="split_name",
            )
            for name, i in (("name", 1), ("second_name", 0))
        ),
    )
    return (
        source,
        db,
        scope,
        TabularImportSuggestion(
            decision="map",
            assignments=choices,
            confidence=Decimal("0.99"),
            reason="semantic_name",
        ),
    )


def test_semantic_copy_and_split_preserve_every_part_and_lineage() -> None:
    source, db, scope, suggestion = sample()
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    result = execute_tabular_import(source.labels, source.rows, db, scope, plan)
    expected: tuple[dict[str, str | None], ...] = (
        {"postal_code": "666333", "name": "Иван", "second_name": "Иванов"},
        {"postal_code": "001000", "name": "Пётр", "second_name": "Петров"},
    )
    assert result.rows == expected
    assert result.lineage[1].source_id == "s1" and result.lineage[1].part_index == 1
    assert result.preview[0].before == source.rows[0]
    assert result.preview[0].after == result.rows[0]
    assert result.input_fingerprint == source.fingerprint
    assert "Иван" not in repr(result)


def test_wire_json_is_strict_and_accepts_numeric_confidence() -> None:
    _, _, _, suggestion = sample()
    wire = suggestion.model_dump_json().replace('"0.99"', "0.99")
    checked = TabularImportSuggestion.model_validate_json(wire)
    assert checked.confidence == Decimal("0.99")


@pytest.mark.parametrize("raw", ["Иванов Иван Иванович", "Иванов", " "])
def test_split_must_fit_all_rows_without_dropping_parts(raw: str) -> None:
    source, db, scope, suggestion = sample()
    rows: tuple[dict[str, str | None], ...] = (
        source.rows[0],
        {"Индекс": "190000", "Фамилия_имя": raw},
    )
    source = TabularImportSource(labels=source.labels, rows=rows)
    with pytest.raises(TabularImportError) as error:
        bind_tabular_import_plan(source, db, scope, suggestion)
    assert error.value.error_code == "TABULAR_IMPORT_SPLIT_PART_COUNT"
    assert error.value.details["row_index"] == 2
    assert error.value.details["source_id"] == "s1"
    if raw.strip():
        assert raw not in str(error.value.details)


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("missing_part", "TABULAR_IMPORT_SPLIT_COVERAGE"),
        ("missing_source", "TABULAR_IMPORT_SOURCE_COVERAGE"),
        ("collision", "TABULAR_IMPORT_TARGET_COLLISION"),
        ("unknown_target", "TABULAR_IMPORT_TARGET_UNKNOWN"),
        ("unknown_source", "TABULAR_IMPORT_SOURCE_UNKNOWN"),
        ("low_confidence", "TABULAR_IMPORT_CONFIDENCE_LOW"),
        ("ambiguous", "TABULAR_IMPORT_NEEDS_REVIEW"),
    ],
)
def test_unsafe_or_partial_plan_is_rejected(kind: str, code: str) -> None:
    source, db, scope, suggestion = sample()
    data = suggestion.model_dump()
    assignments = list(data["assignments"])
    if kind == "missing_part":
        assignments.pop()
    elif kind == "missing_source":
        assignments.pop(0)
    elif kind == "collision":
        assignments[2]["target_id"] = assignments[1]["target_id"]
    elif kind == "unknown_target":
        assignments[0]["target_id"] = "c99"
    elif kind == "unknown_source":
        assignments[0]["source_id"] = "s99"
    elif kind == "low_confidence":
        assignments[0]["confidence"] = Decimal("0.84")
    elif kind == "ambiguous":
        data["decision"] = "ambiguous"
    data["assignments"] = assignments
    with pytest.raises(TabularImportError, match=code):
        bind_tabular_import_plan(
            source, db, scope, TabularImportSuggestion.model_validate(data)
        )


def test_changed_source_scope_or_plan_fingerprint_cannot_be_reused() -> None:
    source, db, scope, suggestion = sample()
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    rows: tuple[dict[str, str | None], ...] = (
        {"Индекс": "190000", "Фамилия_имя": "Иванов Иван"},
    )
    with pytest.raises(TabularImportError, match="TABULAR_IMPORT_BINDING_MISMATCH"):
        execute_tabular_import(source.labels, rows, db, scope, plan)
    forged = plan.model_copy(update={"confidence": Decimal("0.9")})
    with pytest.raises(TabularImportError, match="TABULAR_IMPORT_INPUT_INVALID"):
        execute_tabular_import(source.labels, source.rows, db, scope, forged)


def test_missing_required_target_fails_before_output() -> None:
    source, _, _, suggestion = sample()
    db = catalog(
        table(
            "contacts",
            column("postal_code"),
            column("name"),
            column("second_name"),
            column("required_extra").model_copy(update={"nullable": False}),
        )
    )
    scope = scope_for(db).model_copy(
        update={
            "deny": tuple(
                ref for ref in scope_for(db).allow if ref.column_id == "required_extra"
            )
        }
    )
    with pytest.raises(
        TabularImportError, match="TABULAR_IMPORT_REQUIRED_TARGET_MISSING"
    ):
        bind_tabular_import_plan(source, db, scope, suggestion)


def test_literal_delimiter_and_null_values_are_explicit() -> None:
    source, db, scope, suggestion = sample()
    source = TabularImportSource(
        labels=source.labels,
        rows=(
            {"Индекс": "666333", "Фамилия_имя": "Иванов|Иван"},
            {"Индекс": None, "Фамилия_имя": None},
        ),
    )
    suggestion = suggestion.model_copy(
        update={
            "assignments": tuple(
                c.model_copy(update={"split_mode": "literal", "delimiter": "|"})
                if c.operation == "split"
                else c
                for c in suggestion.assignments
            )
        }
    )
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    result = execute_tabular_import(source.labels, source.rows, db, scope, plan)
    assert result.rows[0]["name"] == "Иван"
    assert result.rows[1] == {"postal_code": None, "name": None, "second_name": None}


def test_plan_roundtrip_has_stable_fingerprint() -> None:
    source, db, scope, suggestion = sample()
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    assert TabularImportPlan.model_validate_json(plan.model_dump_json()) == plan


def test_general_composite_value_splits_into_city_and_country() -> None:
    source = TabularImportSource(
        labels=("Город_страна",), rows=({"Город_страна": "Казань|Россия"},)
    )
    db = catalog(table("places", column("city"), column("country")))
    scope = scope_for(db)
    suggestion = TabularImportSuggestion(
        decision="map",
        confidence=Decimal("0.98"),
        reason="semantic_equivalence",
        assignments=tuple(
            TabularSplitChoice(
                source_id="s0",
                target_id=f"c{i}",
                operation="split",
                split_mode="literal",
                delimiter="|",
                part_index=i,
                part_count=2,
                confidence=Decimal("0.98"),
                reason="composite_component",
            )
            for i in range(2)
        ),
    )
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    result = execute_tabular_import(source.labels, source.rows, db, scope, plan)
    assert result.rows == ({"city": "Казань", "country": "Россия"},)


def test_low_confidence_points_to_assignment_before_overall_plan() -> None:
    source, db, scope, suggestion = sample()
    suggestion = suggestion.model_copy(
        update={
            "confidence": Decimal("0.7"),
            "assignments": (
                suggestion.assignments[0].model_copy(
                    update={"confidence": Decimal("0.8")}
                ),
                *suggestion.assignments[1:],
            ),
        }
    )
    with pytest.raises(
        TabularImportError, match="TABULAR_IMPORT_CONFIDENCE_LOW"
    ) as error:
        bind_tabular_import_plan(source, db, scope, suggestion)
    assert error.value.details["source_id"] == "s0"
    assert error.value.details["actual_confidence"] == "0.8"
    assert error.value.details["min_confidence"] == "0.85"


@pytest.mark.parametrize(
    "kind", ["copy_parameters", "missing_split_part", "mixed_split_operations"]
)
def test_operation_diagnostics_show_closed_parameters_without_raw_values(
    kind: str,
) -> None:
    source, db, scope, suggestion = sample()
    plan = bind_tabular_import_plan(source, db, scope, suggestion)
    wire = plan.model_dump(exclude={"fingerprint"})
    if kind == "copy_parameters":
        wire["assignments"][0]["part_index"] = 0
        wire["assignments"][0]["delimiter"] = "canary"
    elif kind == "missing_split_part":
        wire["assignments"] = wire["assignments"][:-1]
    else:
        wire["assignments"][2]["operation"] = "copy"
    invalid = TabularImportPlan.model_validate(wire)
    with pytest.raises(TabularImportError) as captured:
        execute_tabular_import(source.labels, source.rows, db, scope, invalid)
    details = captured.value.details
    actual, expected = details["actual"], details["expected"]
    assert isinstance(actual, Mapping) and isinstance(expected, Mapping)
    if kind == "copy_parameters":
        assert captured.value.error_code == "TABULAR_IMPORT_OPERATION_INVALID"
        assert actual["part_index"] == 0 and expected["part_index"] is None
        assert actual["delimiter_supplied"] is True
        assert expected["delimiter_supplied"] is False
    elif kind == "missing_split_part":
        assert captured.value.error_code == "TABULAR_IMPORT_SPLIT_COVERAGE"
        assert details["expected_parts"] == 2 and details["actual_parts"] == 1
        assert actual["part_indices"] == (1,)
        assert expected["part_indices"] == (0, 1)
    else:
        assert captured.value.error_code == "TABULAR_IMPORT_SPLIT_COVERAGE"
        parameters = actual["assignments"]
        assert isinstance(parameters, tuple)
        assert isinstance(parameters[1], Mapping)
        assert parameters[1]["operation"] == "copy"
        assert expected["operation"] == "split"
    assert "canary" not in str(details) and "Иванов Иван" not in str(details)
