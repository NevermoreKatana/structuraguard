"""Run-local budget и safe history поверх неизменного provider contract."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

from structuraguard.contracts.llm import LLMBudget, LLMCallRecord, LLMErrorCode
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm._boundary import (
    call_record,
    checked_capabilities,
    checked_generation,
    checked_request,
)
from structuraguard.ports.llm import LLMProvider


def utc_now() -> datetime:
    """Вернуть timezone-aware UTC; в тестах часы внедряются явно."""
    return datetime.now(UTC)


class LLMRunProvider:
    """Один provider, конечный run budget, без retry и изменения capability identity."""

    def __init__(
        self,
        provider: LLMProvider,
        budget: LLMBudget,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._caps = checked_capabilities(provider.capabilities)
        self._budget = LLMBudget.model_validate(budget.model_dump())
        self._clock, self._monotonic = clock, monotonic
        self._start = monotonic()
        self._reserved = 0
        self._calls: list[LLMCallRecord] = []
        self._busy = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Capability identity underlying provider остаётся неизменной."""
        return self._caps

    @property
    def calls(self) -> tuple[LLMCallRecord, ...]:
        """Вернуть safe records всех фактических попыток текущего run."""
        return tuple(self._calls)

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Зарезервировать calls/tokens/deadline до provider invocation."""
        checked = checked_request(request)
        caps = checked_capabilities(self._provider.capabilities)
        if caps != self._caps or self._busy:
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        if (
            caps.max_input_tokens is None
            or caps.max_output_tokens is None
            or caps.context_window_tokens is None
        ):
            raise LLMProviderError(LLMErrorCode.CAPABILITY_MISMATCH)
        allowance = caps.max_input_tokens + caps.max_output_tokens
        remaining = self._budget.max_time_ms / 1000 - (self._monotonic() - self._start)
        if (
            len(self._calls) >= self._budget.max_calls
            or not allowance
            or self._reserved + allowance > self._budget.max_tokens
            or remaining <= 0
        ):
            raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
        self._reserved += allowance
        self._busy = True
        started, tick = self._clock(), self._monotonic()
        response: LLMResponse | None = None
        error_code: LLMErrorCode | None = None
        try:
            async with asyncio.timeout(remaining):
                candidate = await checked_generation(self._provider, checked)
                try:
                    if type(candidate) is not LLMResponse:
                        raise ValueError
                    candidate = LLMResponse.model_validate(
                        candidate.model_dump(warnings="error")
                    )
                    if (
                        candidate.request_id,
                        candidate.response_schema_id,
                        candidate.response_schema_version,
                        candidate.prompt,
                        candidate.provider_id,
                        candidate.provider_version,
                        candidate.model_id,
                    ) != (
                        checked.request_id,
                        checked.response_schema_id,
                        checked.response_schema_version,
                        checked.prompt,
                        caps.provider_id,
                        caps.provider_version,
                        caps.model_id,
                    ):
                        raise ValueError
                    if (
                        candidate.input_tokens > (caps.max_input_tokens or 0)
                        or candidate.output_tokens > (caps.max_output_tokens or 0)
                        or len(candidate.output_json.encode())
                        > min(checked.max_output_bytes, caps.max_output_bytes)
                    ):
                        raise ValueError
                    if (
                        candidate.input_tokens + candidate.output_tokens
                        > caps.context_window_tokens
                    ):
                        raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
                except (ValueError, TypeError, AttributeError, RecursionError):
                    raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE) from None
                response = candidate
                if self._monotonic() - self._start >= self._budget.max_time_ms / 1000:
                    raise TimeoutError
                return response
        except TimeoutError:
            error_code = LLMErrorCode.TIMEOUT
            response = None
            raise LLMProviderError(error_code) from None
        except LLMProviderError as error:
            error_code = LLMErrorCode(error.error_code)
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            error_code = LLMErrorCode.UNAVAILABLE
            response = None
            raise LLMProviderError(error_code) from None
        finally:
            self._busy = False
            self._calls.append(
                call_record(
                    checked,
                    caps,
                    attempt=len(self._calls) + 1,
                    started_at=started,
                    elapsed_ms=min(
                        300000, max(0, int((self._monotonic() - tick) * 1000))
                    ),
                    response=response,
                    error=error_code,
                    reserved_tokens=allowance,
                )
            )
