"""Повторная проверка DTO и safe metadata на provider/router boundary."""

import asyncio
from datetime import datetime, timedelta

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.llm import LLMCallRecord, LLMErrorCode
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.ports.llm import LLMProvider


def sanitized_provider_error(error: LLMProviderError) -> LLMProviderError:
    """Сохранить только allowlisted code чужой ошибки, без notes/cause/call/details."""
    try:
        code = LLMErrorCode(error.error_code)
    except (ValueError, TypeError):
        code = LLMErrorCode.INVALID_RESPONSE
    return LLMProviderError(code)


async def checked_generation(provider: LLMProvider, request: LLMRequest) -> LLMResponse:
    """Локализовать внешнюю generation: exception text может содержать source/secrets."""
    try:
        return await provider.generate_structured(request)
    except LLMProviderError as error:
        raise sanitized_provider_error(error) from None
    except TimeoutError:
        raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
    except asyncio.CancelledError:
        raise asyncio.CancelledError from None
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise LLMProviderError(LLMErrorCode.UNAVAILABLE) from None


def checked_request(request: LLMRequest) -> LLMRequest:
    if type(request) is not LLMRequest:
        raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
    try:
        result = LLMRequest.model_validate(request.model_dump(warnings="error"))
    except (ValueError, TypeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.REQUEST_INVALID) from None
    if result.prompt is None:
        raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
    return result


def checked_capabilities(caps: ProviderCapabilities) -> ProviderCapabilities:
    """Проверить caps до fingerprint, не раскрывая forged fields в warnings."""
    try:
        if type(caps) is not ProviderCapabilities:
            raise ValueError
        return ProviderCapabilities.model_validate(caps.model_dump(warnings="error"))
    except (ValueError, TypeError, RecursionError):
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None


def call_record(
    request: LLMRequest,
    caps: ProviderCapabilities,
    *,
    attempt: int,
    started_at: datetime,
    elapsed_ms: int,
    response: LLMResponse | None = None,
    error: LLMErrorCode | None = None,
    fallback_reason: LLMErrorCode | None = None,
    reserved_tokens: int = 0,
) -> LLMCallRecord:
    assert request.prompt is not None
    return LLMCallRecord(
        attempt=attempt,
        request_fingerprint=canonical_sha256_value(request),
        capabilities_fingerprint=canonical_sha256_value(caps),
        prompt=request.prompt,
        outcome="success"
        if response is not None
        else "failed"
        if error is not None
        else "cancelled",
        error_code=error,
        generation_fingerprint=response.generation_fingerprint if response else None,
        input_tokens=response.input_tokens if response else None,
        output_tokens=response.output_tokens if response else None,
        elapsed_ms=elapsed_ms,
        started_at=started_at,
        finished_at=started_at + timedelta(milliseconds=elapsed_ms),
        provider_id=caps.provider_id,
        provider_version=caps.provider_version,
        model_id=caps.model_id,
        fallback_reason=fallback_reason,
        reserved_tokens=reserved_tokens,
    )
