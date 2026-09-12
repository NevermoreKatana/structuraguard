"""M12 AC-06/08: недостоверные snapshots остаются диагностируемыми."""

from datetime import UTC, datetime

import pytest
from tests.fakes.provenance import provenance_fixture, rehash

from structuraguard.contracts.common import ValidationDecision
from structuraguard.contracts.provenance import (
    DetailedValidationReport,
    ProvenanceLimits,
    ProvenancePolicy,
)
from structuraguard.exceptions import ValidationError
from structuraguard.validation import ProvenanceValidator

_NOW = datetime(2026, 9, 13, tzinfo=UTC)


@pytest.mark.anyio
@pytest.mark.parametrize("identity", ["record_id", "entity_id", "value_id"])
async def test_rehashed_cross_batch_duplicate_ids_return_failed_report(
    identity: str,
) -> None:
    fixture = await provenance_fixture()
    first, second = fixture.normalized[:2]
    record = second.records[0]
    entity = record.entities[0]
    if identity == "record_id":
        record = record.model_copy(update={"record_id": first.records[0].record_id})
    elif identity == "entity_id":
        entity = entity.model_copy(
            update={"entity_id": first.records[0].entities[0].entity_id}
        )
        record = record.model_copy(update={"entities": (entity,)})
    else:
        value = entity.values[0].model_copy(
            update={"value_id": first.records[0].entities[0].values[0].value_id}
        )
        entity = entity.model_copy(update={"values": (value,)})
        record = record.model_copy(update={"entities": (entity,)})
    altered = second.model_copy(update={"records": (record,)})
    report = await ProvenanceValidator().validate(
        rehash((first, altered, *fixture.normalized[2:])),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.REJECTED
    assert report.invalid_records == report.total_records == 2
    assert not report.complete and report.verified_values == 0
    assert [issue.code for issue in report.issues] == ["PROVENANCE_STREAM_INVALID"]
    assert (
        DetailedValidationReport.model_validate_json(report.model_dump_json()) == report
    )
    if identity == "value_id":
        from pydantic import ValidationError as ContractError

        payload = report.model_dump(mode="python")
        payload["evidence"][0]["verified"] = True
        payload.update(verified_values=1, evidence_fingerprint="sha256:" + "0" * 64)
        assert report.evidence[0].value_id == report.evidence[1].value_id
        with pytest.raises(
            ContractError, match="Verified evidence требует уникальные value IDs"
        ):
            DetailedValidationReport.model_validate(payload)


@pytest.mark.anyio
@pytest.mark.parametrize("field", ["extraction_fingerprint", "parse_plan_fingerprint"])
async def test_rehashed_foreign_normalized_lineage_cannot_pass(field: str) -> None:
    fixture = await provenance_fixture()
    other = "sha256:" + "f" * 64
    batches = tuple(
        batch.model_copy(update={field: other}) for batch in fixture.normalized
    )
    manifest = batches[-1].manifest
    assert manifest is not None
    batches = (
        *batches[:-1],
        batches[-1].model_copy(
            update={"manifest": manifest.model_copy(update={field: other})}
        ),
    )
    report = await ProvenanceValidator().validate(
        rehash(batches),
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.REJECTED
    assert "PROVENANCE_STRUCTURE_MISMATCH" in {issue.code for issue in report.issues}
    assert not any(item.verified for item in report.evidence)


@pytest.mark.anyio
async def test_forged_parse_validation_fingerprint_requires_fresh_replay() -> None:
    fixture = await provenance_fixture()
    plan = fixture.plan.model_copy(
        update={"validation_fingerprint": "sha256:" + "f" * 64}
    )
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert report.decision is ValidationDecision.REJECTED
    assert report.verified_values == 0
    assert [issue.code for issue in report.issues] == ["PROVENANCE_REPLAY_FAILED"]


@pytest.mark.anyio
async def test_provenance_issue_budget_never_returns_partial_success() -> None:
    fixture = await provenance_fixture()
    batches = []
    for batch in fixture.normalized:
        records = tuple(
            record.model_copy(
                update={
                    "entities": tuple(
                        entity.model_copy(
                            update={
                                "values": tuple(
                                    value.model_copy(
                                        update={"origins": (), "selection": None}
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
        batches.append(batch.model_copy(update={"records": records}))
    with pytest.raises(ValidationError) as error:
        await ProvenanceValidator(
            policy=ProvenancePolicy(limits=ProvenanceLimits(max_issues=1))
        ).validate(
            rehash(tuple(batches)),
            source_batches=fixture.physical,
            plan=fixture.plan,
            context=fixture.context,
            generated_at=_NOW,
        )
    assert error.value.error_code == "SECURITY_LIMIT_EXCEEDED"


@pytest.mark.anyio
async def test_replay_cleanup_failure_after_terminal_discards_verified_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterable, AsyncIterator
    from contextlib import aclosing

    from structuraguard.contracts.execution import ExecutionStage, ParseExecutionIssue
    from structuraguard.contracts.normalized import NormalizedBatch
    from structuraguard.contracts.parsing import (
        ParseExecutionContext,
        ValidatedParsePlan,
    )
    from structuraguard.contracts.source import ExtractedBatch
    from structuraguard.exceptions import ParseExecutionError
    from structuraguard.structure import ParsePlanExecutor

    fixture = await provenance_fixture()
    original = ParsePlanExecutor.execute
    terminal_seen: list[bool] = []

    async def faulty(
        self: ParsePlanExecutor,
        batches: AsyncIterable[ExtractedBatch],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncIterator[NormalizedBatch]:
        async with aclosing(original(self, batches, plan, context)) as source:
            async for batch in source:
                if batch.is_last:
                    terminal_seen.append(True)
                yield batch
        raise ParseExecutionError(
            ParseExecutionIssue(
                code="PARSE_EXECUTION_CLEANUP_FAILED",
                reason="cleanup",
                stage=ExecutionStage.CLEANUP,
            )
        )

    monkeypatch.setattr(ParsePlanExecutor, "execute", faulty)
    report = await ProvenanceValidator().validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
    )
    assert terminal_seen == [True]
    assert not report.complete and report.verified_values == 0
    assert report.decision is ValidationDecision.REJECTED
    assert [issue.code for issue in report.issues] == ["PROVENANCE_REPLAY_FAILED"]


@pytest.mark.anyio
@pytest.mark.parametrize("part", ["registry", "policy", "step"])
async def test_untrusted_normalization_bindings_never_reach_normalizer(
    part: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from structuraguard.contracts.normalization import (
        NormalizationPolicy,
        NormalizationResult,
        NormalizerSpec,
    )
    from structuraguard.contracts.normalized import SemanticFieldRef
    from structuraguard.contracts.profiling import LocalePolicy
    from structuraguard.contracts.provenance import NormalizationBinding
    from structuraguard.normalization import (
        NormalizerRegistry,
        NormalizerRegistrySnapshot,
    )

    fixture = await provenance_fixture()
    registry = NormalizerRegistry.with_builtins().freeze()
    value = fixture.normalized[0].records[0].entities[0].values[0]
    steps = (NormalizerSpec(normalizer_id="trim"),)
    result = registry.normalize_value(value, steps=steps)
    if part == "registry":
        result = result.model_copy(
            update={"registry_fingerprint": "sha256:" + "f" * 64}
        )
    elif part == "policy":
        result = result.model_copy(
            update={"policy": NormalizationPolicy(locale=LocalePolicy.EN_GB)}
        )
    else:
        forged = NormalizerSpec(normalizer_id="trim", version="2.0.0")
        result = result.model_copy(
            update={
                "requested_steps": (forged,),
                "steps": (result.steps[0].model_copy(update={"spec": forged}),),
            }
        )

    def forbidden(*args: object, **kwargs: object) -> NormalizationResult:
        pytest.fail("Непривязанный trace не разрешает вызов normalizer")

    monkeypatch.setattr(NormalizerRegistrySnapshot, "normalize_value", forbidden)
    binding = NormalizationBinding(
        field=SemanticFieldRef(entity_type="unresolved", field_name="field_0"),
        steps=steps,
    )
    report = await ProvenanceValidator(
        policy=ProvenancePolicy(normalizations=(binding,)), registry=registry
    ).validate(
        fixture.normalized,
        source_batches=fixture.physical,
        plan=fixture.plan,
        context=fixture.context,
        generated_at=_NOW,
        normalizations=(result,),
    )
    assert report.decision is ValidationDecision.REJECTED
    assert report.invalid_records == 1
    assert "NORMALIZATION_EVIDENCE_MISMATCH" in {issue.code for issue in report.issues}
