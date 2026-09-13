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
from structuraguard.contracts.security import Resource
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm._boundary import (
    call_record,
    checked_capabilities,
    checked_generation,
    checked_request,
    enforce_security_route,
)
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.resources import LLMResourceGuard


def utc_now() -> datetime:
    """Вернуть timezone-aware UTC; в тестах часы внедряются явно."""
    return datetime.now(UTC)


class LLMRunProvider:
    """Один provider с конечным budget, без retry и изменения capability identity.

    Args:
        provider: Явный LLMProvider; wrapper не владеет его lifecycle.
        budget: Локальные calls/tokens/time caps; создание начинает deadline.
        clock: UTC часы для call metadata.
        monotonic: Монотонные секунды для deadline.
        resources: Необязательный общий LLMResourceGuard, только сужающий caps.

    Constructor читает capabilities, не делает generation/network I/O.
    Неверный budget даёт ValidationError; provider/request ошибки — LLMProviderError.
    Общий guard может дать SecurityPolicyError. Резерв каждой попытки не возвращается
    при ошибке/cancel. Restricted cloud и injection restrictions проверяются до
    provider call. Wrapper не добавляет tools/DB/SQL capability.
    """

    def __init__(
        self,
        provider: LLMProvider,
        budget: LLMBudget,
        *,
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        resources: LLMResourceGuard | None = None,
    ) -> None:
        self._provider = provider
        self._caps = checked_capabilities(provider.capabilities)
        self._budget = LLMBudget.model_validate(budget.model_dump())
        self._clock, self._monotonic = clock, monotonic
        self._start = monotonic()
        self._reserved = 0
        self._calls: list[LLMCallRecord] = []
        self._busy = False
        self._resources = resources

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
        if self._resources is not None:
            return await self._resources.call(
                lambda: self._generate_structured(request),
                resource=Resource.LLM_TIME_MS,
            )
        return await self._generate_structured(request)

    async def _generate_structured(self, request: LLMRequest) -> LLMResponse:
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
        enforce_security_route(checked, caps)
        allowance = caps.max_input_tokens + caps.max_output_tokens
        remaining = self._budget.max_time_ms / 1000 - (self._monotonic() - self._start)
        if (
            len(self._calls) >= self._budget.max_calls
            or not allowance
            or self._reserved + allowance > self._budget.max_tokens
            or remaining <= 0
        ):
            raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
        if self._resources is not None:
            remaining = min(remaining, self._resources.llm_remaining_seconds())
            self._resources.reserve_llm(allowance)
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
