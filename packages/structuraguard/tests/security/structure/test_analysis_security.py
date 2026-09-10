"""Недоверенные profiles/plans не становятся execution authority."""

import asyncio
import socket
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError
from tests.security.structure.test_profile_security import batches_for, stream
from tests.unit.contracts.test_m02_semantic_contract_regressions import (
    _manifest,
    _profile,
    _sample,
)
from tests.unit.structure.test_analysis import analyze_content

from structuraguard.contracts.analysis import StructureAnalysisOptions
from structuraguard.contracts.common import SemanticParsingMode
from structuraguard.contracts.parsing import (
    ParsePlan,
    StructureAnalysisRequest,
    StructurePlanCreated,
    StructureProfile,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.contracts.structure import StructuralProfilingOptions
from structuraguard.exceptions import SecurityPolicyError, StructuralAnalysisError
from structuraguard.parsers.builtin import (
    DelimitedTextParser,
    JsonDocumentParser,
    PlainTextParser,
)
from structuraguard.structure import DeterministicStructureAnalyzer, StructuralProfiler


@pytest.mark.anyio
async def test_profile_requires_replay_and_recomputed_forgery_is_rejected() -> None:
    batches = await batches_for(b"metric cpu 1\nmetric cpu 2\n")
    profile = await StructuralProfiler().profile(stream(batches))
    result = await DeterministicStructureAnalyzer().analyze(profile)
    assert result.kind == "needs_semantic_analysis"
    assert result.issues[0].code == "STRUCTURE_REPLAY_REQUIRED"
    valid = await DeterministicStructureAnalyzer().analyze(
        profile, batches=stream(batches)
    )
    assert isinstance(valid, StructurePlanCreated)
    changed = profile.model_dump(mode="python")
    changed["observations"][0]["confidence"] = Decimal("0.1")
    changed["profile_fingerprint"] = "sha256:" + "0" * 64
    forged = StructureProfile.model_validate(changed)
    with pytest.raises(StructuralAnalysisError) as failure:
        await DeterministicStructureAnalyzer().analyze(forged, batches=stream(batches))
    assert failure.value.details["reason"] == "profile_replay_mismatch"


@pytest.mark.anyio
async def test_forged_profile_preflight_and_model_construct() -> None:
    profile = _profile()
    forged = profile.model_copy(update={"profile_id": "secret-canary" * 100_000})
    with pytest.raises(SecurityPolicyError) as failure:
        await DeterministicStructureAnalyzer(
            options=StructureAnalysisOptions(
                profiling=StructuralProfilingOptions(max_batch_bytes=4096)
            )
        ).analyze(forged)
    assert "secret-canary" not in str(failure.value)
    with pytest.raises(StructuralAnalysisError):
        await DeterministicStructureAnalyzer().analyze(
            StructureProfile.model_construct()
        )


@pytest.mark.anyio
async def test_protocol_request_and_unsupported_mode_never_fallback() -> None:
    manifest = _manifest()
    for mode in SemanticParsingMode:
        request = StructureAnalysisRequest(
            source=manifest.source,
            manifest=manifest,
            profile=_profile(),
            samples=(_sample("cell-1"),),
            mode=mode,
        )
        result = await DeterministicStructureAnalyzer().analyze(request)
        if mode is SemanticParsingMode.DETERMINISTIC:
            assert result.kind == "needs_semantic_analysis"
        else:
            assert result.kind == "rejected"
            assert result.issues[0].code == "STRUCTURE_MODE_UNSUPPORTED"


@pytest.mark.anyio
async def test_no_network_and_source_code_is_inert_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network call")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    content = (
        b'[{"__import__(x); SELECT * FROM t":1},{"__import__(x); SELECT * FROM t":2}]'
    )
    result = await analyze_content(JsonDocumentParser(), content)
    assert isinstance(result, StructurePlanCreated)
    encoded = result.plan.model_dump_json()
    assert "SELECT * FROM t" in encoded
    assert "regex" not in type(result.plan).model_fields


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["regex", "python", "sql", "shell", "callback"])
async def test_unknown_operators_and_plan_fields_are_forbidden(operation: str) -> None:
    result = await analyze_content(JsonDocumentParser(), b'[{"id":1},{"id":2}]')
    assert isinstance(result, StructurePlanCreated)
    payload = result.plan.model_dump(mode="python", exclude={"fingerprint"})
    payload["fields"][0]["selector"]["steps"][0]["operation"] = operation
    with pytest.raises(ValidationError):
        TypeAdapter(ParsePlan).validate_python(payload)
    payload = result.plan.model_dump(mode="python", exclude={"fingerprint"})
    payload[operation] = "secret-canary"
    with pytest.raises(ValidationError):
        TypeAdapter(ParsePlan).validate_python(payload)


@pytest.mark.anyio
async def test_partial_samples_never_authorize_an_unseen_range() -> None:
    result = await analyze_content(
        DelimitedTextParser(),
        b"name,count\n" + b"Ada,1\n" * 300,
        options=StructureAnalysisOptions(
            confidence_threshold=Decimal(0),
            profiling=StructuralProfilingOptions(max_sample_items=8),
        ),
    )
    assert result.kind != "plan_created"
    assert result.profile.coverage is not None and not result.profile.coverage.complete
    assert all(
        c.assessment is not None and "incomplete_coverage" in c.assessment.blockers
        for c in result.profile.candidates
    )


@pytest.mark.anyio
async def test_unknown_log_scope_and_ambiguous_footer_do_not_disappear() -> None:
    result = await analyze_content(
        PlainTextParser(), b"metric cpu 1\nmetric cpu 2\nunknown prose\n"
    )
    assert result.kind == "needs_semantic_analysis"
    assert result.issues[0].code == "STRUCTURE_UNHANDLED_SCOPE"
    result = await analyze_content(
        DelimitedTextParser(), b"name,count\nAda,1\nBob,2\nTotal,999\n"
    )
    assert result.kind != "plan_created"
    assert result.profile.candidates[0].assessment is not None
    assert "unconfirmed_footer" in result.profile.candidates[0].assessment.blockers


@pytest.mark.anyio
async def test_tree_missing_fields_require_explicit_policy() -> None:
    result = await analyze_content(
        JsonDocumentParser(), b'[{"id":1,"name":"Ada"},{"id":2}]'
    )
    assert result.kind == "needs_semantic_analysis"
    assert result.profile.candidates[0].assessment is not None
    assert (
        "optional_field_policy_required"
        in result.profile.candidates[0].assessment.blockers
    )


@pytest.mark.anyio
async def test_analysis_cancellation_closes_stream() -> None:
    batches = await batches_for(b"metric cpu 1\nmetric cpu 2\n")
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def source() -> AsyncGenerator[ExtractedBatch, None]:
        try:
            yield batches[0]
            entered.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(DeterministicStructureAnalyzer().analyze(source()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


@pytest.mark.anyio
async def test_nested_array_containers_are_not_flattened_without_policy() -> None:
    result = await analyze_content(JsonDocumentParser(), b"[[1,2],[3,4]]")
    assert result.kind == "needs_semantic_analysis"
    assert result.profile.candidates[0].assessment is not None
    assert (
        "array_container_policy_required"
        in result.profile.candidates[0].assessment.blockers
    )


@pytest.mark.anyio
async def test_ranked_log_candidates_report_unhandled_scope() -> None:
    result = await analyze_content(
        PlainTextParser(),
        b"metric cpu 1\nmetric cpu 2\nINFO x=1\nINFO x=2\nunknown prose\n",
    )
    assert result.kind == "needs_review"
    assert "STRUCTURE_UNHANDLED_SCOPE" in {issue.code for issue in result.issues}
