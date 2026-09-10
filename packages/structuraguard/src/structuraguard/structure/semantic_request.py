"""Exact payload approval для structural и document semantic requests."""

from structuraguard.contracts._base import (
    CanonicalValue,
    canonical_json_value,
    canonical_sha256_value,
)
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.llm import LLMErrorCode, LLMExecutionEnvironment
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    SecurityApproval,
    SecurityReport,
    SecurityScanRequest,
)
from structuraguard.contracts.semantic import LLMAnalysisContext
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm import LLMPromptTemplate, LLMResponseSchema
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.security import SecurityScanner
from structuraguard.structure.plan_compilation import reject_active_content


async def approved_request(
    payload: dict[str, CanonicalValue],
    *,
    source_fingerprint: str,
    context: LLMAnalysisContext,
    scanner: SecurityScanner,
    prompt: LLMPromptTemplate,
    schema: LLMResponseSchema,
    max_input_bytes: int,
    max_output_bytes: int,
) -> LLMRequest:
    """Scanner разрешает только точный bounded payload с доверенной routing policy."""
    encoded = canonical_json_value(payload)
    if len(encoded.encode()) > max_input_bytes:
        raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
    reject_active_content(encoded)
    fingerprint = canonical_sha256_value(payload)
    scan = SecurityScanRequest(
        request_id="scan_" + fingerprint[-24:],
        run_id=context.run_id,
        purpose="llm_input",
        content_fingerprint=source_fingerprint,
        payload_json=encoded,
        payload_fingerprint=fingerprint,
        data_classification=context.data_classification,
        routing_policy_id=context.routing_policy_id,
        routing_policy_fingerprint=context.routing_policy_fingerprint,
        redaction_fingerprint=context.redaction_fingerprint,
    )
    try:
        report = await scanner.scan(scan)
    except TimeoutError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None
    try:
        report = SecurityReport.model_validate(report.model_dump(warnings="error"))
        return LLMRequest(
            request_id="semantic_" + fingerprint[-24:],
            run_id=context.run_id,
            purpose="semantic_parsing",
            response_schema_id=schema.schema_id,
            response_schema_version=schema.version,
            payload_json=encoded,
            payload_fingerprint=fingerprint,
            content_fingerprint=source_fingerprint,
            data_classification=context.data_classification,
            routing_policy_id=context.routing_policy_id,
            routing_policy_fingerprint=context.routing_policy_fingerprint,
            redaction_fingerprint=context.redaction_fingerprint,
            security_approval=SecurityApproval(
                report=report, report_fingerprint=canonical_sha256_value(report)
            ),
            prompt=prompt.identity,
            prompt_fingerprint=prompt.identity.fingerprint,
            max_output_bytes=max_output_bytes,
        )
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None


def checked_destination(
    provider: LLMProvider, request: LLMRequest
) -> ProviderCapabilities:
    """Неизвестная locality и restricted cloud запрещены до вызова provider."""
    try:
        caps = ProviderCapabilities.model_validate(
            provider.capabilities.model_dump(warnings="error")
        )
    except (ValueError, TypeError, AttributeError):
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None
    if caps.execution_environment not in {
        LLMExecutionEnvironment.LOCAL,
        LLMExecutionEnvironment.CLOUD,
    } or (
        request.data_classification is DataClassification.RESTRICTED
        and caps.execution_environment is not LLMExecutionEnvironment.LOCAL
    ):
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
    if not caps.structured_output or request.purpose not in caps.supported_purposes:
        raise LLMProviderError(LLMErrorCode.CAPABILITY_MISMATCH)
    if len(request.payload_json.encode()) > caps.max_input_bytes:
        raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
    return caps


def checked_output(
    response: LLMResponse, request: LLMRequest, caps: ProviderCapabilities
) -> LLMResponse:
    """Проверить source-independent response binding до разбора semantic schema."""
    try:
        if (
            type(response) is not LLMResponse
            or len(response.output_json) > request.max_output_bytes
        ):
            raise ValueError
        result = LLMResponse.model_validate(response.model_dump(warnings="error"))
        if (
            result.request_id,
            result.response_schema_id,
            result.response_schema_version,
            result.prompt,
            result.provider_id,
            result.provider_version,
            result.model_id,
        ) != (
            request.request_id,
            request.response_schema_id,
            request.response_schema_version,
            request.prompt,
            caps.provider_id,
            caps.provider_version,
            caps.model_id,
        ):
            raise ValueError
        if len(result.output_json.encode()) > request.max_output_bytes:
            raise ValueError
        reject_active_content(result.output_json)
        return result
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.SCHEMA_VIOLATION) from None
