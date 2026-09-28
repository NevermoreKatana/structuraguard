"""Цельные scalars не становятся составными только из-за пунктуации."""

import json
from decimal import Decimal

import pytest
from tests.fakes.mapping import catalog, column, scope_for, table
from tests.unit.mapping.test_tabular_planner import output, planner

from structuraguard.contracts._base import CanonicalValue, canonical_json_value
from structuraguard.contracts.tabular_import import (
    TabularImportOptions,
    TabularImportSource,
    TabularImportSuggestion,
    TabularLiteralSplitChoice,
)
from structuraguard.mapping._tabular_values import atomic_value_kind
from structuraguard.mapping.tabular import _samples, _value_hint
from structuraguard.mapping.tabular_execution import (
    TabularImportError,
    bind_tabular_import_plan,
    execute_tabular_import,
)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("123e4567-e89b-42d3-a456-426614174000", "uuid"),
        ("2026-09-28", "date"),
        ("18:42:16.123456", "time"),
        ("18:42:16+03:00", "time"),
        ("-12.50", "decimal"),
        ("Иванов Иван", None),
        ("Иванов-Иван", None),
        ("2026-02-30", None),
        ("26:99:61", None),
        ("001234", None),
        ("1.2.3", None),
    ],
)
def test_closed_atomic_shapes(value: str, kind: str | None) -> None:
    assert atomic_value_kind(value) == kind


@pytest.mark.anyio
async def test_atomic_samples_do_not_advertise_punctuation_splits() -> None:
    values: dict[str, str | None] = {
        "request": "123e4567-e89b-42d3-a456-426614174000",
        "calendar": "2026-09-28",
        "clock": "18:42:16.123456",
        "amount": "12.50",
    }
    labels = tuple(values)
    db = catalog(table("events", *(column(label) for label in labels)))
    body = json.loads(output(target="c0"))
    template = body["assignments"][0]
    body["assignments"] = [
        {
            **template,
            "source_id": f"s{i}",
            "target_id": f"c{sorted(labels).index(label)}",
        }
        for i, label in enumerate(labels)
    ]
    sdk, scanner = planner(canonical_json_value(body))
    plan = await sdk.plan(
        labels=labels, rows=(values,), catalog=db, scope=scope_for(db)
    )
    fields = json.loads(scanner.payloads[0])["source_fields"]
    assert [field["value_kind"] for field in fields] == [
        "uuid",
        "date",
        "time",
        "decimal",
    ]
    assert all(field["split_candidates"] == [] for field in fields)
    assert execute_tabular_import(labels, (values,), db, scope_for(db), plan).rows == (
        values,
    )


def test_truncated_and_mixed_samples_do_not_claim_an_atomic_kind() -> None:
    source = TabularImportSource(
        labels=("value",), rows=({"value": "2026-09-28"}, {"value": "not-a-date"})
    )
    assert _value_hint("value", source, _samples(source, TabularImportOptions())) == {}
    truncated = TabularImportSource(
        labels=("value",), rows=({"value": "2026-09-28-extra"},)
    )
    options = TabularImportOptions(max_sample_chars=10)
    assert _value_hint("value", truncated, _samples(truncated, options)) == {}


@pytest.mark.parametrize(
    ("kind", "value", "code"),
    [
        ("integer", "worker-one", "NORMALIZATION_INVALID_VALUE"),
        ("integer", "00123", "NORMALIZATION_LOSSY_CONVERSION"),
        ("integer", "2147483648", "DB_NUMERIC_BOUNDS"),
        ("uuid", "123e4567", "DB_VALUE_INVALID"),
        ("date", "18:42:16.123456", "DB_TYPE_MISMATCH"),
        ("date", "2026-02-30", "DB_TYPE_MISMATCH"),
    ],
)
def test_target_value_failure_has_coordinates_and_no_raw_value(
    kind: str, value: str, code: str
) -> None:
    db = catalog(table("events", column("destination", kind)))
    source = TabularImportSource(labels=("source",), rows=({"source": value},))
    suggestion = TabularImportSuggestion.model_validate_json(output(target="c0"))
    with pytest.raises(TabularImportError) as caught:
        bind_tabular_import_plan(source, db, scope_for(db), suggestion)
    exc = caught.value
    assert exc.error_code == "TABULAR_IMPORT_TARGET_VALUE_INVALID"
    assert exc.details["source_id"] == "s0"
    assert exc.details["target_column_id"] == "destination"
    assert exc.details["row_index"] == 1
    assert exc.details["actual"] == {"validation_codes": (code,), "operation": "copy"}
    assert exc.details["expected"] == {"canonical_type": kind}
    assert value not in str(exc.details)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("integer", "123"),
        ("integer", "-2147483648"),
        ("date", "2026-09-28"),
        ("uuid", "123e4567-e89b-42d3-a456-426614174000"),
    ],
)
def test_target_check_retains_exact_original_output(kind: str, value: str) -> None:
    db = catalog(table("events", column("destination", kind)))
    source = TabularImportSource(labels=("source",), rows=({"source": value},))
    suggestion = TabularImportSuggestion.model_validate_json(output(target="c0"))
    plan = bind_tabular_import_plan(source, db, scope_for(db), suggestion)
    result = execute_tabular_import(source.labels, source.rows, db, scope_for(db), plan)
    assert result.rows == ({"destination": value},)


def test_explicit_meaningful_date_components_remain_supported() -> None:
    db = catalog(
        table(
            "parts",
            column("year", "integer"),
            column("month", "text"),
            column("day", "text"),
        )
    )
    source = TabularImportSource(
        labels=("calendar",), rows=({"calendar": "2026-09-28"},)
    )
    suggestion = TabularImportSuggestion(
        decision="map",
        confidence=Decimal(".99"),
        reason="composite_component",
        assignments=tuple(
            TabularLiteralSplitChoice(
                source_id="s0",
                target_id=f"c{target}",
                operation="split",
                split_mode="literal",
                delimiter="-",
                part_count=3,
                part_index=index,
                confidence=Decimal(".99"),
                reason="composite_component",
            )
            for index, target in enumerate((2, 1, 0))
        ),
    )
    plan = bind_tabular_import_plan(source, db, scope_for(db), suggestion)
    assert execute_tabular_import(
        source.labels, source.rows, db, scope_for(db), plan
    ).rows == ({"year": "2026", "month": "09", "day": "28"},)


@pytest.mark.anyio
async def test_typed_target_feedback_requests_a_new_meaningful_plan() -> None:
    db = catalog(table("events", column("id", "integer"), column("message", "text")))
    sdk, scanner = planner(output(target="c0"), output(target="c1"))
    plan = await sdk.plan(
        labels=("message",),
        rows=({"message": "Job started"},),
        catalog=db,
        scope=scope_for(db),
    )
    assert len(scanner.payloads) == 2
    feedback = json.loads(scanner.payloads[1])["validation_feedback"]
    assert feedback["code"] == "TABULAR_IMPORT_TARGET_VALUE_INVALID"
    assert feedback["source_id"] == "s0" and feedback["target_id"] == "c0"
    assert feedback["expected"] == {"canonical_type": "integer"}
    assert feedback["actual"]["validation_codes"] == ["NORMALIZATION_INVALID_VALUE"]
    assert plan.assignments[0].target.column_id == "message"


def test_typed_target_guard_checks_later_rows_before_publishing() -> None:
    db = catalog(table("events", column("destination", "integer")))
    rows: tuple[dict[str, str | None], ...] = tuple(
        {"source": "worker" if index == 18 else "12"} for index in range(19)
    )
    source = TabularImportSource(labels=("source",), rows=rows)
    suggestion = TabularImportSuggestion.model_validate_json(output(target="c0"))
    with pytest.raises(TabularImportError) as caught:
        bind_tabular_import_plan(source, db, scope_for(db), suggestion)
    assert caught.value.error_code == "TABULAR_IMPORT_TARGET_VALUE_INVALID"
    assert caught.value.details["row_index"] == 19


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("123e4567-e89b-42d3-a456-426614174000", ["c1", "c3", "c4", "c5"]),
        ("2026-09-28", ["c2", "c3", "c4", "c5"]),
        ("12", ["c0", "c3", "c4", "c5"]),
        ("18:42:16.123456", ["c3", "c4", "c5"]),
        ("worker", ["c3", "c4", "c5"]),
        (None, ["c0", "c1", "c2", "c3", "c4", "c5"]),
    ],
)
def test_copy_type_evidence_excludes_only_proven_incompatible_targets(
    value: str | None, allowed: list[str]
) -> None:
    from structuraguard.mapping.tabular import _copy_type_targets

    columns = (
        column("count", "integer"),
        column("identifier", "uuid"),
        column("calendar", "date"),
        column("description", "text"),
        column("other", "unknown"),
        column("unverified").model_copy(update={"inspection": None}),
    )
    source = TabularImportSource(labels=("value",), rows=({"value": value},))
    assert (
        _copy_type_targets(
            "value",
            source,
            _samples(source, TabularImportOptions()),
            columns,
            "postgresql",
        )
        == allowed
    )


def test_copy_type_evidence_does_not_test_truncated_original_values() -> None:
    from structuraguard.mapping.tabular import _copy_type_targets

    source = TabularImportSource(labels=("value",), rows=({"value": "123-invalid"},))
    samples = _samples(source, TabularImportOptions(max_sample_chars=3))
    columns = (column("count", "integer"), column("calendar", "date"))
    assert _copy_type_targets("value", source, samples, columns, "postgresql") == [
        "c0",
        "c1",
    ]


def test_one_full_incompatible_sample_is_sufficient_copy_type_evidence() -> None:
    from structuraguard.mapping.tabular import _copy_type_targets

    source = TabularImportSource(
        labels=("value",), rows=({"value": "123"}, {"value": "worker"})
    )
    assert (
        _copy_type_targets(
            "value",
            source,
            _samples(source, TabularImportOptions()),
            (column("count", "integer"),),
            "postgresql",
        )
        == []
    )


@pytest.mark.anyio
async def test_whole_value_type_evidence_does_not_restrict_split_components() -> None:
    from structuraguard.mapping.tabular import tabular_import_response_schema

    db = catalog(
        table("parts", column("first", "integer"), column("second", "integer"))
    )
    body: CanonicalValue = {
        "fields": [
            {
                "source_id": "s0",
                "explanation": "Two integer components have separate destinations.",
                "operation": "split",
                "target_ids": ["c0", "c1"],
                "split_mode": "literal",
                "delimiter": "|",
                "confidence": "0.99",
            }
        ],
        "confidence": "0.99",
        "decision": "map",
    }
    answer = canonical_json_value(body)
    sdk, scanner = planner(answer)
    rows: tuple[dict[str, str | None], ...] = ({"pair": "12|34"},)
    plan = await sdk.plan(labels=("pair",), rows=rows, catalog=db, scope=scope_for(db))
    payload = json.loads(scanner.payloads[0])
    assert payload["source_fields"][0]["copy_type_compatible_target_ids"] == []
    assert [target["target_id"] for target in payload["target_columns"]] == ["c0", "c1"]
    tabular_import_response_schema().prepare_decoding(scanner.payloads[0]).validate(
        answer
    )
    assert execute_tabular_import(("pair",), rows, db, scope_for(db), plan).rows == (
        {"first": "12", "second": "34"},
    )
