"""Фиксированные IDs, время и security evidence для provider contract tests."""

import hashlib
from datetime import UTC, datetime

from structuraguard.contracts import (
    DataClassification,
    LLMRequest,
    PipelineStatus,
    ProducerMetadata,
    SecurityApproval,
    SecurityReport,
)
from structuraguard.contracts.llm import LLMPrompt

NOW = datetime(2026, 9, 10, 12, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def request_for(payload: str = '{"sample":"masked"}') -> LLMRequest:
    fingerprint = digest(payload)
    report = SecurityReport(
        request_id="scan-1",
        run_id="run-1",
        purpose="llm_input",
        content_fingerprint=digest("source"),
        payload_fingerprint=fingerprint,
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="local-1",
        routing_policy_fingerprint=digest("policy"),
        redaction_fingerprint=digest("redaction"),
        producer=ProducerMetadata(
            component_id="test_scanner", component_version="1.0.0", sdk_version="0.3.0"
        ),
        decision="allowed",
        status=PipelineStatus.COMPLETED,
        artifact_fingerprints=(digest("source"), fingerprint),
        scanned_items=1,
        generated_at=NOW,
    )
    return LLMRequest(
        request_id="request-1",
        run_id="run-1",
        purpose="semantic_parsing",
        response_schema_id="test-result",
        response_schema_version="1.0.0",
        payload_json=payload,
        payload_fingerprint=fingerprint,
        content_fingerprint=report.content_fingerprint,
        data_classification=report.data_classification,
        routing_policy_id=report.routing_policy_id,
        routing_policy_fingerprint=report.routing_policy_fingerprint,
        redaction_fingerprint=report.redaction_fingerprint,
        security_approval=SecurityApproval(
            report=report, report_fingerprint=digest(report.canonical_json())
        ),
        prompt_fingerprint=digest("prompt v1"),
        prompt=LLMPrompt(
            prompt_id="semantic_parse_plan",
            version="1.0.0",
            fingerprint=digest("prompt v1"),
        ),
        max_output_bytes=4096,
    )
