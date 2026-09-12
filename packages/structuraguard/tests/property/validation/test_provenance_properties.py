"""Unicode locations и перестановки findings не меняют verified semantics."""

import json
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fakes.provenance import provenance_fixture

from structuraguard.contracts.common import IssueSeverity, ValidationDecision
from structuraguard.contracts.provenance import (
    ValidationFinding,
    ValidationLayer,
    ValidationLayerResult,
)
from structuraguard.validation import ProvenanceValidator, ValidationReportBuilder


@pytest.mark.anyio
@settings(max_examples=20, deadline=None)
@given(
    key=st.text(
        alphabet=st.characters(exclude_categories=["Cs"]), min_size=1, max_size=20
    )
)
async def test_unicode_json_pointer_round_trip_has_exact_locations(key: str) -> None:
    fixture = await provenance_fixture(
        json.dumps([{key: "one"}, {key: "two"}]).encode()
    )
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    assert report.decision is ValidationDecision.ACCEPTED
    expected = tuple(
        origin.location
        for batch in fixture.normalized
        for record in batch.records
        for entity in record.entities
        for value in entity.values
        for origin in value.origins
    )
    assert (
        tuple(
            location.location
            for evidence in report.evidence
            for location in evidence.locations
        )
        == expected
    )
    assert type(report).model_validate_json(report.model_dump_json()) == report


@pytest.mark.anyio
@settings(max_examples=12, deadline=None)
@given(permutation=st.permutations((0, 1, 2, 3)))
async def test_issue_permutations_preserve_order_hash_and_aggregates(
    permutation: list[int],
) -> None:
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=datetime(2026, 9, 12, tzinfo=UTC),
    )
    findings = (
        ValidationFinding(
            layer=ValidationLayer.JSON_SCHEMA,
            code="SCHEMA_TYPE",
            record_index=1,
            check_index=2,
        ),
        ValidationFinding(
            layer=ValidationLayer.JSON_SCHEMA,
            code="SCHEMA_TYPE",
            record_index=0,
            check_index=2,
        ),
        ValidationFinding(
            layer=ValidationLayer.JSON_SCHEMA,
            code="SCHEMA_REQUIRED",
            record_index=0,
            check_index=1,
        ),
        ValidationFinding(
            layer=ValidationLayer.JSON_SCHEMA,
            code="SCHEMA_WARNING",
            record_index=1,
            severity=IssueSeverity.WARNING,
            outcome="warning",
        ),
    )
    result = ValidationLayerResult(
        layer=ValidationLayer.JSON_SCHEMA,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "d" * 64,),
        complete=True,
        findings=findings,
    )
    builder = ValidationReportBuilder(required_layers=(ValidationLayer.JSON_SCHEMA,))
    first = builder.combine(report, layers=(result,))
    second = builder.combine(
        report,
        layers=(
            result.model_copy(
                update={"findings": tuple(findings[i] for i in permutation)}
            ),
        ),
    )
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert first.invalid_records == first.total_records == 2
    assert first.warning_records == 1
