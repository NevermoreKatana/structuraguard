"""Forgery, resource и privacy regressions physical provenance."""

from datetime import UTC, datetime

import pytest
from tests.fakes.provenance import provenance_fixture, rehash, replace_value

from structuraguard.contracts.common import StringScalar, ValidationDecision
from structuraguard.contracts.provenance import ProvenanceLimits, ProvenancePolicy
from structuraguard.exceptions import ValidationError
from structuraguard.validation import ProvenanceValidator

_NOW = datetime(2026, 9, 12, tzinfo=UTC)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "field,replacement",
    [("artifact_id", "foreign_source"), ("source_fingerprint", "sha256:" + "f" * 64)],
)
async def test_foreign_source_even_with_consistent_hashes(
    field: str, replacement: str
) -> None:
    fixture = await provenance_fixture()
    other = fixture.normalized[0].source.model_copy(update={field: replacement})
    altered = []
    for batch in fixture.normalized:
        records = tuple(
            record.model_copy(
                update={
                    "entities": tuple(
                        entity.model_copy(
                            update={
                                "values": tuple(
                                    value.model_copy(
                                        update={
                                            "origins": tuple(
                                                origin.model_copy(
                                                    update={
                                                        "location": origin.location.model_copy(
                                                            update={"source": other}
                                                        )
                                                    }
                                                )
                                                for origin in value.origins
                                            )
                                        }
                                    )
                                    for value in entity.values
                                )
                            }
                        )
                        for entity in record.entities
                    )
                }
            )
            for record in batch.records
        )
        altered.append(batch.model_copy(update={"source": other, "records": records}))
    report = await ProvenanceValidator().validate(
        rehash(tuple(altered)),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert "PROVENANCE_SOURCE_MISMATCH" in {issue.code for issue in report.issues}
    assert report.decision is ValidationDecision.REJECTED
    assert report.invalid_records == report.total_records
    assert not any(item.verified for item in report.evidence)


@pytest.mark.anyio
@pytest.mark.parametrize("target", ["raw", "selection", "unknown_ref"])
async def test_forged_value_evidence_is_rejected(target: str) -> None:
    fixture = await provenance_fixture()
    value = fixture.normalized[0].records[0].entities[0].values[0]
    if target == "raw":
        raw = StringScalar(value="forged")
        value = value.model_copy(
            update={
                "raw_value": raw,
                "normalized_value": raw,
                "origins": tuple(
                    o.model_copy(update={"raw_value": raw}) for o in value.origins
                ),
            }
        )
        code = "PROVENANCE_RAW_MISMATCH"
    elif target == "selection":
        assert value.selection is not None
        value = value.model_copy(
            update={
                "selection": value.selection.model_copy(
                    update={"selector_fingerprint": "sha256:" + "f" * 64}
                )
            }
        )
        code = "PROVENANCE_SELECTION_MISMATCH"
    else:
        ref = value.source_refs[0].model_copy(update={"local_id": "node-999"})
        # Entity/record refs должны покрывать extra value refs; это нормальное дерево DTO.
        value = value.model_copy(update={"source_refs": (*value.source_refs, ref)})
        batch = fixture.normalized[0]
        record = batch.records[0]
        entity = record.entities[0]
        entity = entity.model_copy(
            update={"source_refs": (*entity.source_refs, ref), "values": (value,)}
        )
        record = record.model_copy(
            update={"source_refs": (*record.source_refs, ref), "entities": (entity,)}
        )
        batches = rehash(
            (batch.model_copy(update={"records": (record,)}), *fixture.normalized[1:])
        )
        report = await ProvenanceValidator().validate(
            batches,
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )
        assert "PROVENANCE_REF_UNKNOWN" in {issue.code for issue in report.issues}
        return
    report = await ProvenanceValidator().validate(
        replace_value(fixture, value),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert code in {issue.code for issue in report.issues}
    assert report.invalid_records == 1


@pytest.mark.anyio
async def test_late_physical_failure_cannot_publish_verified_evidence() -> None:
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical[:-1],
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.REJECTED
    assert not report.complete
    assert report.verified_values == 0
    assert "PROVENANCE_REPLAY_FAILED" in {issue.code for issue in report.issues}


@pytest.mark.anyio
async def test_nonterminal_normalized_stream_cannot_pass() -> None:
    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized[:-1],
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.REJECTED
    assert not report.complete
    assert "PROVENANCE_STREAM_INVALID" in {issue.code for issue in report.issues}


@pytest.mark.anyio
async def test_snapshot_budgets_fail_closed() -> None:
    fixture = await provenance_fixture()
    with pytest.raises(ValidationError, match="SECURITY_LIMIT_EXCEEDED"):
        await ProvenanceValidator(
            policy=ProvenancePolicy(limits=ProvenanceLimits(max_values=1))
        ).validate(
            fixture.normalized,
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )


@pytest.mark.anyio
async def test_forged_subclass_serializer_is_never_called() -> None:
    from pydantic import model_serializer

    fixture = await provenance_fixture()
    calls = []

    class HostileScalar(StringScalar):
        @model_serializer
        def leak(self) -> str:
            calls.append("executed")
            return "restricted@example.org"

    value = fixture.normalized[0].records[0].entities[0].values[0]
    forged = value.model_copy(update={"raw_value": HostileScalar(value="secret")})
    record = fixture.normalized[0].records[0]
    entity = record.entities[0].model_copy(update={"values": (forged,)})
    batch = fixture.normalized[0].model_copy(
        update={"records": (record.model_copy(update={"entities": (entity,)}),)}
    )
    with pytest.raises(ValidationError, match="PROVENANCE_INPUT_INVALID"):
        await ProvenanceValidator().validate(
            (batch, *fixture.normalized[1:]),
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )
    assert not calls


@pytest.mark.anyio
async def test_self_consistent_forged_normalization_trace_is_not_authority() -> None:
    from structuraguard.contracts.normalization import (
        NormalizerOutput,
        NormalizerSpec,
        ValueTransformation,
    )
    from structuraguard.contracts.normalized import SemanticFieldRef
    from structuraguard.contracts.provenance import NormalizationBinding
    from structuraguard.normalization import NormalizerRegistry

    fixture = await provenance_fixture()
    registry = NormalizerRegistry.with_builtins().freeze()
    binding = NormalizationBinding(
        field=SemanticFieldRef(entity_type="unresolved", field_name="field_0"),
        steps=(NormalizerSpec(normalizer_id="trim"),),
    )
    value = fixture.normalized[0].records[0].entities[0].values[0]
    trace = registry.normalize_value(value, steps=binding.steps)
    wrong = StringScalar(value="invented")
    step = trace.steps[0].model_copy(
        update={
            "output": NormalizerOutput(
                value=wrong,
                transformations=(
                    ValueTransformation(
                        operation="trim",
                        input_value=trace.input_value,
                        output_value=wrong,
                    ),
                ),
            )
        }
    )
    forged = trace.model_copy(update={"steps": (step,), "normalized_value": wrong})
    # Это валидная связная цепочка DTO, но trim не выдаёт такой результат.
    assert type(trace).model_validate(forged.model_dump(mode="python")) == forged
    report = await ProvenanceValidator(
        policy=ProvenancePolicy(normalizations=(binding,)), registry=registry
    ).validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
        normalizations=(forged,),
    )
    assert "NORMALIZATION_EVIDENCE_MISMATCH" in {issue.code for issue in report.issues}
    assert report.invalid_records == report.unresolved_records == 1
    assert report.decision is ValidationDecision.REJECTED


@pytest.mark.anyio
async def test_cancelled_replay_closes_iterator_and_does_not_return_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from collections.abc import AsyncIterable, AsyncIterator
    from contextlib import aclosing

    from structuraguard.contracts.normalized import NormalizedBatch
    from structuraguard.contracts.parsing import (
        ParseExecutionContext,
        ValidatedParsePlan,
    )
    from structuraguard.contracts.source import ExtractedBatch
    from structuraguard.structure import ParsePlanExecutor

    fixture = await provenance_fixture()
    started = asyncio.Event()
    closed = asyncio.Event()
    original = ParsePlanExecutor.execute

    async def controlled(
        self: ParsePlanExecutor,
        batches: AsyncIterable[ExtractedBatch],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncIterator[NormalizedBatch]:
        try:
            async with aclosing(original(self, batches, plan, context)) as iterator:
                async for batch in iterator:
                    started.set()
                    yield batch
                    await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(ParsePlanExecutor, "execute", controlled)
    task = asyncio.create_task(
        ProvenanceValidator().validate(
            fixture.normalized,
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.anyio
async def test_report_rejects_forged_counters_and_unknown_value_ordinals() -> None:
    from pydantic import ValidationError as ContractError

    from structuraguard.contracts.provenance import (
        DetailedValidationReport,
        ValidationFinding,
        ValidationLayer,
        ValidationLayerResult,
    )
    from structuraguard.validation import ValidationReportBuilder

    fixture = await provenance_fixture()
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    payload = report.model_dump(mode="python")
    payload.update(verified_values=0, evidence_fingerprint="sha256:" + "0" * 64)
    with pytest.raises(ContractError):
        DetailedValidationReport.model_validate(payload)
    forged = ValidationLayerResult(
        layer=ValidationLayer.BUSINESS_RULES,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "d" * 64,),
        complete=True,
        findings=(
            ValidationFinding(
                layer=ValidationLayer.BUSINESS_RULES,
                code="INVALID_FIELD",
                record_index=0,
                entity_index=0,
                value_index=999,
            ),
        ),
    )
    with pytest.raises((ValidationError, ContractError)):
        ValidationReportBuilder(
            required_layers=(ValidationLayer.BUSINESS_RULES,)
        ).combine(report, layers=(forged,))


@pytest.mark.anyio
async def test_locations_are_never_opened_and_safe_summary_hides_arbitrary_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins
    import socket

    from structuraguard.contracts.provenance import (
        ValidationFinding,
        ValidationLayer,
        ValidationLayerResult,
    )
    from structuraguard.validation import ValidationReportBuilder

    fixture = await provenance_fixture()

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Provenance не должен выполнять file/network I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    issue = ValidationFinding(
        layer=ValidationLayer.JSON_SCHEMA,
        code="RESTRICTED_CUSTOM_CANARY",
        json_path="$.restricted@example.org",
    )
    layer = ValidationLayerResult(
        layer=ValidationLayer.JSON_SCHEMA,
        input_fingerprint=report.lineage.input_fingerprint,
        evidence_fingerprints=("sha256:" + "f" * 64,),
        complete=True,
        findings=(issue,),
    )
    combined = ValidationReportBuilder(
        required_layers=(ValidationLayer.JSON_SCHEMA,)
    ).combine(report, layers=(layer,))
    safe = combined.safe_summary().model_dump_json()
    assert "RESTRICTED" not in safe
    assert "restricted" not in safe
    assert "sha256" not in safe
    assert "execution_test" not in safe
    assert combined.safe_summary().issue_counts[0].code == "VALIDATION_ISSUE"


@pytest.mark.anyio
async def test_forged_extra_fields_are_not_silently_dropped() -> None:
    fixture = await provenance_fixture()
    batch = fixture.normalized[0].model_copy(
        update={"untrusted_extension": "restricted"}
    )
    with pytest.raises(ValidationError, match="PROVENANCE_INPUT_INVALID"):
        await ProvenanceValidator().validate(
            (batch, *fixture.normalized[1:]),
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )


@pytest.mark.anyio
async def test_missing_evidence_is_reported_even_when_physical_replay_fails() -> None:
    fixture = await provenance_fixture()
    value = (
        fixture.normalized[0]
        .records[0]
        .entities[0]
        .values[0]
        .model_copy(update={"origins": (), "selection": None})
    )
    report = await ProvenanceValidator().validate(
        replace_value(fixture, value),
        source_batches=(),
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert {"PROVENANCE_REQUIRED", "PROVENANCE_REPLAY_FAILED"} <= {
        issue.code for issue in report.issues
    }
