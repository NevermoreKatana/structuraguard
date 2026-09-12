"""Regression tests для trust boundaries, выявленных security review M10."""

import asyncio
import re
import traceback

import pytest
from tests.fakes.mapping import catalog, column, profile, scope_for, table
from tests.fakes.semantic_mapping import (
    RecordingScanner,
    decision_for,
    fake_provider,
    mapping_options,
    router_for,
    run_mapper,
)

from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.contracts.semantic_mapping import SemanticMappingContext
from structuraguard.exceptions import LLMProviderError, SecurityPolicyError
from structuraguard.mapping import LLMSemanticMapper, prepare_semantic_mapping

pytestmark = pytest.mark.anyio
CANARY = "private-source-content-m10-review"


class ExternalScanFailure(Exception):
    """Тип исключения стороннего scanner, неизвестный ядру."""


class FailingScanner(RecordingScanner):
    def __init__(self, failure: str) -> None:
        super().__init__()
        self.failure = failure

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        if self.failure == "lookup":
            raise KeyError(CANARY)
        if self.failure == "cancelled":
            raise asyncio.CancelledError(CANARY)
        if self.failure == "typed":
            raise SecurityPolicyError(error_code="SCANNER_FAILED", message=CANARY)
        error = ExternalScanFailure(CANARY)
        error.add_note(CANARY)
        raise error


@pytest.mark.parametrize("failure", ["lookup", "typed", "custom", "cancelled"])
async def test_scanner_exception_text_never_escapes_m10_boundary(
    failure: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")))
    provider = fake_provider("{}")
    router = router_for(provider)
    mapper = LLMSemanticMapper(
        router=router,
        scanner=FailingScanner(failure),
        context=SemanticMappingContext(run_id="mapping-run"),
    )
    expected = asyncio.CancelledError if failure == "cancelled" else LLMProviderError
    with pytest.raises(expected) as error:
        await mapper.propose(data, db, scope=scope_for(db))
    assert CANARY not in "".join(traceback.format_exception(error.value)) + caplog.text
    if isinstance(error.value, LLMProviderError):
        assert error.value.error_code == "LLM_POLICY_DENIED"
    assert provider.call_count == router.reserved_tokens == 0


@pytest.mark.parametrize("value", ["a" * 4096, "я" * 300], ids=["ascii", "utf8_bytes"])
async def test_overlong_metadata_never_enters_redaction_regex(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await profile()
    db = catalog(
        table("contacts", column("email").model_copy(update={"comment": value}))
    )
    original = re.subn
    seen: list[int] = []

    def bounded_redaction(pattern: str, replacement: str, text: str) -> tuple[str, int]:
        seen.append(len(text.encode()))
        assert len(text.encode()) <= 512, "Metadata exceeds bounded redaction input"
        return original(pattern, replacement, text)

    # Проверяем лимит работы у дорогой операции; wall-clock threshold не нужен.
    monkeypatch.setattr(re, "subn", bounded_redaction)
    prepared = await prepare_semantic_mapping(data, db, scope=scope_for(db))
    assert seen and max(seen) <= 512
    assert value not in prepared.groups[0].payload_json
    assert "[REDACTED]" in prepared.groups[0].payload_json


@pytest.mark.parametrize("location", ["choice", "selected_assessment"])
@pytest.mark.parametrize(
    "reason", ["MULTIPLE_PLAUSIBLE_TARGETS", "INSUFFICIENT_EVIDENCE", "NO_MATCH"]
)
async def test_model_uncertainty_reason_cannot_be_hidden_by_selected_status(
    location: str,
    reason: str,
) -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0])
    choice = decision.columns[0]
    if location == "choice":
        choice = choice.model_copy(update={"reason_code": reason})
    else:
        choice = choice.model_copy(
            update={
                "assessments": tuple(
                    a.model_copy(update={"reason_code": reason})
                    if a.candidate_id == choice.selected_candidate_id
                    else a
                    for a in choice.assessments
                )
            }
        )
    decision = decision.model_copy(update={"columns": (choice,)})
    result, _, _ = await run_mapper(data, db, decision)
    assert result.action == "confirm"
    assert result.status.value == "NEEDS_REVIEW"
    if reason == "MULTIPLE_PLAUSIBLE_TARGETS":
        assert result.groups[0].ambiguous
        assert "AMBIGUOUS_TARGET" in result.groups[0].reasons
    else:
        assert "MODEL_REQUESTED_REVIEW" in result.groups[0].reasons


@pytest.mark.parametrize(
    "reason", ["MULTIPLE_PLAUSIBLE_TARGETS", "INSUFFICIENT_EVIDENCE"]
)
async def test_unassessed_alternative_cannot_be_dismissed_by_low_model_score(
    reason: str,
) -> None:
    data = await profile()
    db = catalog(table("contacts", column("email")), table("archive", column("email")))
    prepared = await prepare_semantic_mapping(
        data, db, scope=scope_for(db), ranking_options=mapping_options()
    )
    decision = decision_for(prepared.groups[0])
    choice = decision.columns[0]
    decision = decision.model_copy(
        update={
            "columns": (
                choice.model_copy(
                    update={
                        "assessments": tuple(
                            a.model_copy(update={"reason_code": reason})
                            if a.candidate_id != choice.selected_candidate_id
                            else a
                            for a in choice.assessments
                        )
                    }
                ),
            )
        }
    )
    result, _, _ = await run_mapper(data, db, decision)
    assert result.action == "confirm" and result.status.value == "NEEDS_REVIEW"
