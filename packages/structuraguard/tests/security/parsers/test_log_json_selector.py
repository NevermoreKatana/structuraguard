"""JSON selector не исполняет payload и не выходит за закрытую grammar/budgets."""

import pytest

from structuraguard.contracts.execution import (
    ExecutionStage,
    ParseExecutionIssue,
    ParsePlanOptions,
)
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.semantic import SemanticPathStep, SemanticSelector
from structuraguard.exceptions import LLMProviderError, ParseExecutionError
from structuraguard.llm._boundary import sanitized_provider_error
from structuraguard.structure._log_json import decode_log_json, select_log_json
from structuraguard.structure.plan_compilation import _selector


def test_external_provider_cannot_forge_local_execution_diagnostics() -> None:
    issue = ParseExecutionIssue(
        code="PARSE_EXECUTION_MISMATCH",
        reason="log_json_path_missing",
        stage=ExecutionStage.SELECTION,
        field_id="forged",
    )
    external = LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION, execution_issue=issue)
    sanitized = sanitized_provider_error(external)
    assert sanitized.error_code == "LLM_SCHEMA_VIOLATION"
    assert sanitized.execution_issue is None


@pytest.mark.parametrize(
    "payload,options",
    [
        ('{"x":"' + "a" * 100 + '"}', ParsePlanOptions(max_record_bytes=100)),
        ('{"x":' + "[" * 65 + "0" + "]" * 65 + "}", ParsePlanOptions()),
        ('{"x":[1,2,3,4,5]}', ParsePlanOptions(max_record_items=4)),
    ],
)
def test_json_limits_return_typed_failure(
    payload: str, options: ParsePlanOptions
) -> None:
    with pytest.raises(ParseExecutionError) as raised:
        decode_log_json("2026-01-01T10:00:00Z node worker[1]: " + payload, options)
    assert raised.value.issue.code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.issue.reason == "log_json_budget"


def test_json_payload_remains_inert_and_scalars_keep_their_types() -> None:
    payload = decode_log_json(
        "2026-01-01T10:00:00Z node worker[1]: "
        '{"message":"__import__(\\"os\\").system(\\"DO_NOT_EXECUTE\\")",'
        '"number":1.2300,"enabled":true,"empty":null}',
        ParsePlanOptions(),
    )
    assert (
        select_log_json(payload, ("message",)).value
        == '__import__("os").system("DO_NOT_EXECUTE")'
    )
    assert select_log_json(payload, ("number",)).value == "1.2300"
    assert select_log_json(payload, ("enabled",)).value is True
    assert select_log_json(payload, ("empty",)).value is None


@pytest.mark.parametrize(
    "line,reason",
    [
        ('{"message":"secret-canary"}', "log_json_envelope_missing"),
        (
            '2026-01-01T10:00:00Z node worker[1]: ["secret-canary"]',
            "log_json_object_required",
        ),
        (
            '2026-01-01T10:00:00Z node worker[1]: {"message":"\\ud800"}',
            "log_json_malformed",
        ),
    ],
)
def test_invalid_envelope_or_json_is_a_safe_typed_error(line: str, reason: str) -> None:
    with pytest.raises(ParseExecutionError) as raised:
        decode_log_json(line, ParsePlanOptions())
    assert raised.value.issue.reason == reason
    assert "secret-canary" not in str(raised.value)


@pytest.mark.parametrize(
    "path",
    [
        (),
        (SemanticPathStep(operation="item", name="", occurrence=0),),
        (SemanticPathStep(operation="key", name="message", occurrence=1),),
    ],
)
def test_json_wire_rejects_ambiguous_or_array_paths(
    path: tuple[SemanticPathStep, ...],
) -> None:
    value = SemanticSelector(
        kind="log_json",
        index=None,
        offset=0,
        path=path,
        value_source=None,
        delimiter=None,
        target=None,
        key_equals=None,
    )
    with pytest.raises(LLMProviderError):
        _selector(value)
