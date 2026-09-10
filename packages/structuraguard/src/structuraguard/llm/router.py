"""Run-local routing с immutable destination allowlist и общим бюджетом."""

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import DataClassification
from structuraguard.contracts.llm import (
    LLMCallRecord,
    LLMErrorCode,
    LLMExecutionEnvironment,
    LLMRoutingMode,
    LLMRoutingPolicy,
)
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.ports.llm import LLMProvider

from ._boundary import (
    call_record,
    checked_capabilities,
    checked_generation,
    checked_request,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PolicyAwareLLMRouter:
    """Один run, один event loop, максимум один запрос одновременно.

    SecurityApproval разрешает полный fingerprint policy: порядок deployments,
    class allowlists и budget. Перед каждым fallback повторно проверяются exact
    request и caps; redaction/classification не изменяются. Router не выпускает
    SecurityApproval и не владеет lifecycle providers. Retry одного provider
    отсутствует; следующий разрешённый provider вызывается только при transient
    failure. Резерв max_input_tokens + max_output_tokens не возвращается даже
    при отмене/неизвестном usage. Это консервативный upper bound, не invoice.
    ``providers`` следуют порядку routes в ``policy``; ``run_id`` связывает requests
    с run. ``clock``/``monotonic`` задают UTC timestamps/монотонные секунды. Invalid
    policy/deployments дают ValueError/Pydantic ValidationError без сетевого I/O.
    Новый instance создаёт новый budget. Router не является LLMProvider и напрямую
    не подставляется вместо concrete provider в SemanticParsingSession.
    """

    def __init__(
        self,
        *,
        policy: LLMRoutingPolicy,
        providers: tuple[LLMProvider, ...],
        run_id: str,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._policy = LLMRoutingPolicy.model_validate(
            policy.model_dump(warnings="error")
        )
        if len(providers) != len(policy.routes):
            raise ValueError("Каждой route должен соответствовать один provider")
        self._providers = providers
        self._caps = tuple(
            ProviderCapabilities.model_validate(
                p.capabilities.model_dump(warnings="error")
            )
            for p in providers
        )
        if any(
            canonical_sha256_value(caps) != route.capabilities_fingerprint
            for caps, route in zip(self._caps, policy.routes, strict=True)
        ):
            raise ValueError("Deployment capabilities не соответствуют policy")
        if not run_id or len(run_id) > 128:
            raise ValueError("Требуется bounded run identity")
        self._run_id = run_id
        self._clock = clock
        self._monotonic = monotonic
        self._start = monotonic()
        self._busy = False
        self._calls: list[LLMCallRecord] = []
        self._reserved_tokens = 0
        self._attempt_count = 0

    @property
    def policy(self) -> LLMRoutingPolicy:
        """Полная policy, которую должен разрешить trusted security scanner."""
        return self._policy

    @property
    def policy_fingerprint(self) -> str:
        """Fingerprint без endpoint, credentials, source data и provider handles."""
        return canonical_sha256_value(self._policy)

    @property
    def calls(self) -> tuple[LLMCallRecord, ...]:
        """Safe run metadata, bounded max_calls; preflight denial не есть attempt."""
        return tuple(self._calls)

    @property
    def reserved_tokens(self) -> int:
        """Суммарный неизрасходуемый повторно резерв всех начатых attempts."""
        return self._reserved_tokens

    def _remaining_ms(self) -> int:
        return self._policy.budget.max_time_ms - max(
            0, int((self._monotonic() - self._start) * 1000)
        )

    def _approved(self, request: LLMRequest) -> None:
        if (
            request.run_id != self._run_id
            or request.routing_policy_id != self._policy.policy_id
            or request.routing_policy_fingerprint != self.policy_fingerprint
        ):
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)

    def _candidates(self, request: LLMRequest) -> list[int]:
        candidates: list[int] = []
        for index, (route, caps) in enumerate(
            zip(self._policy.routes, self._caps, strict=True)
        ):
            environment = caps.execution_environment
            if (
                request.data_classification not in route.allowed_classifications
                or environment
                not in {LLMExecutionEnvironment.LOCAL, LLMExecutionEnvironment.CLOUD}
            ):
                continue
            if (
                request.data_classification is DataClassification.RESTRICTED
                and environment is not LLMExecutionEnvironment.LOCAL
            ):
                continue
            if (
                self._policy.mode is LLMRoutingMode.LOCAL_ONLY
                and environment is not LLMExecutionEnvironment.LOCAL
            ):
                continue
            if (
                self._policy.mode is LLMRoutingMode.PRIVACY_FIRST
                and request.data_classification
                in {DataClassification.CONFIDENTIAL, DataClassification.RESTRICTED}
                and environment is not LLMExecutionEnvironment.LOCAL
            ):
                continue
            candidates.append(index)
        if not candidates:
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        if self._policy.mode is LLMRoutingMode.PRIVACY_FIRST:
            candidates.sort(
                key=lambda index: (
                    self._caps[index].execution_environment
                    is not LLMExecutionEnvironment.LOCAL
                )
            )
        compatible = [
            index
            for index in candidates
            if request.purpose in self._caps[index].supported_purposes
            and all(
                value is not None
                for value in (
                    self._caps[index].max_input_tokens,
                    self._caps[index].max_output_tokens,
                    self._caps[index].context_window_tokens,
                )
            )
        ]
        if not compatible:
            raise LLMProviderError(LLMErrorCode.CAPABILITY_MISMATCH)
        return compatible

    def _reserve(self, caps: ProviderCapabilities) -> int:
        assert caps.max_input_tokens is not None and caps.max_output_tokens is not None
        reservation = caps.max_input_tokens + caps.max_output_tokens
        budget = self._policy.budget
        if (
            self._attempt_count >= budget.max_calls
            or self._reserved_tokens + reservation > budget.max_tokens
            or self._remaining_ms() <= 0
        ):
            raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
        self._attempt_count += 1
        self._reserved_tokens += reservation
        return reservation

    def _checked_response(
        self, response: LLMResponse, request: LLMRequest, caps: ProviderCapabilities
    ) -> LLMResponse:
        try:
            if type(response) is not LLMResponse:
                raise ValueError
            result = LLMResponse.model_validate(response.model_dump(warnings="error"))
            if (
                result.request_id,
                result.response_schema_id,
                result.response_schema_version,
                result.prompt,
                result.prompt_fingerprint,
                result.provider_id,
                result.provider_version,
                result.model_id,
            ) != (
                request.request_id,
                request.response_schema_id,
                request.response_schema_version,
                request.prompt,
                request.prompt_fingerprint,
                caps.provider_id,
                caps.provider_version,
                caps.model_id,
            ):
                raise ValueError
            assert (
                caps.max_input_tokens is not None
                and caps.max_output_tokens is not None
                and caps.context_window_tokens is not None
            )
            if (
                len(result.output_json.encode())
                > min(request.max_output_bytes, caps.max_output_bytes)
                or result.input_tokens > caps.max_input_tokens
                or result.output_tokens > caps.max_output_tokens
                or result.input_tokens + result.output_tokens
                > caps.context_window_tokens
            ):
                raise ValueError
            return result
        except (ValueError, TypeError, RecursionError):
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE) from None

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Выбрать разрешённый policy provider и вернуть его проверенный response.

        Request должен иметь exact approval для этого run/policy. Fallback сохраняет
        classification/redaction и происходит только после transient failure.
        Restricted data не уходит cloud; no_llm или отсутствие допустимой route дают
        LLM_POLICY_DENIED. Исчерпание calls/tokens/time и конкурентный запрос дают
        LLM_BUDGET_EXCEEDED через LLMProviderError; прочие typed codes сохраняются.
        Provider diagnostics/notes/cause отбрасываются, safe attempts доступны в calls.
        Cancellation распространяется как asyncio.CancelledError. Router выполняет
        I/O только через providers и не владеет их lifecycle.
        """
        if self._policy.mode is LLMRoutingMode.NO_LLM:
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        checked = checked_request(request)
        self._approved(checked)
        if self._busy:
            raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
        candidates = self._candidates(checked)
        self._busy = True
        previous: LLMErrorCode | None = None
        try:
            for index in candidates:
                caps, provider = self._caps[index], self._providers[index]
                self._approved(checked_request(checked))
                if canonical_sha256_value(
                    checked_capabilities(provider.capabilities)
                ) != canonical_sha256_value(caps):
                    raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
                reservation = self._reserve(caps)
                result: LLMResponse | None = None
                failure: LLMProviderError | None = None
                started_at, start = self._clock(), self._monotonic()
                try:
                    async with asyncio.timeout(self._remaining_ms() / 1000):
                        result = self._checked_response(
                            await checked_generation(provider, checked), checked, caps
                        )
                    if self._remaining_ms() <= 0:
                        result = None
                        raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
                    return result
                except TimeoutError:
                    failure = LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
                except LLMProviderError as error:
                    # Чужой adapter не может протащить raw details/exception chain.
                    try:
                        code = LLMErrorCode(error.error_code)
                    except ValueError:
                        code = LLMErrorCode.INVALID_RESPONSE
                    failure = LLMProviderError(code)
                except asyncio.CancelledError:
                    raise asyncio.CancelledError from None
                finally:
                    record = call_record(
                        checked,
                        caps,
                        attempt=self._attempt_count,
                        started_at=started_at,
                        elapsed_ms=min(
                            300_000, max(0, int((self._monotonic() - start) * 1000))
                        ),
                        response=result,
                        error=LLMErrorCode(failure.error_code) if failure else None,
                        fallback_reason=previous,
                        reserved_tokens=reservation,
                    )
                    self._calls.append(record)
                    if failure is not None:
                        failure.call = record
                assert failure is not None
                previous = LLMErrorCode(failure.error_code)
                if (
                    self._policy.mode is LLMRoutingMode.FIXED
                    or previous
                    not in {
                        LLMErrorCode.TIMEOUT,
                        LLMErrorCode.RATE_LIMIT,
                        LLMErrorCode.UNAVAILABLE,
                    }
                    or index == candidates[-1]
                ):
                    raise failure from None
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        finally:
            self._busy = False
