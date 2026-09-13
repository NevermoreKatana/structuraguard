"""Run-bound injection wrapper поверх разрешающего synthetic scanner."""

from structuraguard.contracts._base import (
    CanonicalValue,
    canonical_json_value,
    canonical_sha256_value,
)
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.injection import InjectionPolicy
from structuraguard.contracts.reports import (
    LLMRequest,
    SecurityApproval,
    SecurityScanRequest,
)
from structuraguard.llm import PolicyAwareLLMRouter
from structuraguard.security.scanner import InjectionAwareSecurityScanner
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner
from tests.unit.llm.test_router import approved_request


def scanner_for(
    router: PolicyAwareLLMRouter, policy: InjectionPolicy | None = None
) -> InjectionAwareSecurityScanner:
    return InjectionAwareSecurityScanner(
        scanner=Scanner(),
        policy=policy or InjectionPolicy(),
        run_id="run-1",
        routing_policy_id=router.policy.policy_id,
        routing_policy_fingerprint=router.policy_fingerprint,
        clock=fixed_clock,
    )


def scan_for(
    request: LLMRequest, payload: dict[str, CanonicalValue]
) -> SecurityScanRequest:
    text = canonical_json_value(payload)
    return SecurityScanRequest(
        request_id=request.request_id,
        run_id=request.run_id,
        purpose="llm_input",
        content_fingerprint=request.content_fingerprint,
        payload_json=text,
        payload_fingerprint=canonical_sha256_value(payload),
        data_classification=request.data_classification,
        routing_policy_id=request.routing_policy_id,
        routing_policy_fingerprint=request.routing_policy_fingerprint,
        redaction_fingerprint=request.redaction_fingerprint,
    )


async def scanned_request(
    router: PolicyAwareLLMRouter,
    scanner: InjectionAwareSecurityScanner,
    *,
    payload: dict[str, CanonicalValue] | None = None,
    classification: DataClassification = DataClassification.INTERNAL,
) -> LLMRequest:
    original = approved_request(router, classification)
    scan = scan_for(original, {"sample": "ordinary"} if payload is None else payload)
    report = await scanner.scan(scan)
    return LLMRequest.model_validate(
        {
            **original.model_dump(),
            "payload_json": scan.payload_json,
            "payload_fingerprint": scan.payload_fingerprint,
            "security_approval": SecurityApproval(
                report=report, report_fingerprint=canonical_sha256_value(report)
            ),
        }
    )
