"""Scripted in-process provider; clocks и checkpoints задаёт trusted caller."""

import asyncio
import hashlib
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StrictStr, TypeAdapter

from structuraguard.contracts._base import FrozenContract, canonical_sha256_value
from structuraguard.contracts.common import FingerprintStr, NonNegativeInt, UtcDateTime
from structuraguard.contracts.llm import (
    LLMCallRecord,
    LLMErrorCode,
    LLMExecutionEnvironment,
)
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.exceptions import LLMProviderError


class ScriptedResponse(FrozenContract):
    """Raw response fixture, включая malformed JSON и inert injected payload.

    output_json намеренно не проверяется до вызова provider. Его содержимое
    не входит в repr/diagnostics. Usage и elapsed_ms заданы сценарием;
    fake не считает tokens и не ожидает реальное время.
    """

    output_json: Annotated[StrictStr, Field(max_length=1_048_576)] = Field(repr=False)
    input_tokens: NonNegativeInt = 0
    output_tokens: NonNegativeInt = 0
    elapsed_ms: Annotated[NonNegativeInt, Field(le=300_000)] = 0
    expected_request_fingerprint: FingerprintStr | None = None


class ScriptedFailure(FrozenContract):
    """Transient failure fixture без exception body или произвольного сообщения."""

    code: Literal[
        LLMErrorCode.TIMEOUT, LLMErrorCode.RATE_LIMIT, LLMErrorCode.UNAVAILABLE
    ]
    elapsed_ms: Annotated[NonNegativeInt, Field(le=300_000)] = 0
    expected_request_fingerprint: FingerprintStr | None = None


class FakeLLMProvider:
    """Потребляет один script step на попытку без сети, retry или hidden fallback.

    Args:
        script: До 1000 typed steps и 4 MiB raw response fixtures суммарно.
        clock: Явные UTC часы; один вызов на начатую попытку.
        capabilities: Byte/token limits и identity; fake не поддерживает JSON Schema.
        max_history: До 1000 последних metadata records (default 64).
        before_response: Optional async checkpoint для controlled cancellation.

    Raises:
        ValueError: Неверная конфигурация fixtures/capabilities/limits.
        LLMProviderError: Нарушен request/response contract либо сценарий исчерпан.

    State принадлежит instance и одному event loop. Step резервируется до await;
    cancellation расходует его. History хранит только metadata, не raw fixtures.
    Prompt metadata обязательна; legacy DTO без неё остаются доступны прежним adapters.
    """

    def __init__(
        self,
        script: tuple[ScriptedResponse | ScriptedFailure, ...],
        *,
        clock: Callable[[], datetime],
        capabilities: ProviderCapabilities | None = None,
        max_history: int = 64,
        before_response: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if type(script) is not tuple or len(script) > 1000:
            raise ValueError("Script должен быть tuple не более чем из 1000 steps")
        if any(
            type(step) not in {ScriptedResponse, ScriptedFailure} for step in script
        ):
            raise ValueError("Неизвестный тип script step")
        if (
            sum(
                len(step.output_json.encode(errors="surrogatepass"))
                for step in script
                if isinstance(step, ScriptedResponse)
            )
            > 4_194_304
        ):
            raise ValueError("Script превышает общий byte budget")
        self._script = tuple(
            type(step).model_validate(step.model_dump(warnings="error"))
            for step in script
        )
        if type(max_history) is not int or not 1 <= max_history <= 1000:
            raise ValueError("max_history вне диапазона 1..1000")
        if not callable(clock) or (
            before_response is not None and not callable(before_response)
        ):
            raise ValueError("Clock/checkpoint должны быть callable")
        defaults = ProviderCapabilities(
            provider_id="fake",
            provider_version="1.0.0",
            model_id="scripted",
            structured_output=True,
            supported_purposes=("semantic_parsing",),
            execution_environment=LLMExecutionEnvironment.LOCAL,
            max_input_bytes=65_536,
            max_output_bytes=65_536,
            max_input_tokens=8192,
            max_output_tokens=2048,
            context_window_tokens=16384,
        )
        if capabilities is not None and type(capabilities) is not ProviderCapabilities:
            raise ValueError("capabilities должны быть ProviderCapabilities")
        self._capabilities = ProviderCapabilities.model_validate(
            (capabilities or defaults).model_dump(warnings="error")
        )
        if (
            self._capabilities.json_schema
            or self._capabilities.tool_calling
            or self._capabilities.execution_environment
            is not LLMExecutionEnvironment.LOCAL
        ):
            raise ValueError(
                "Fake поддерживает local structured JSON без JSON Schema/tools"
            )
        self._clock = clock
        self._checkpoint = before_response
        self._offset = 0
        self._history: deque[LLMCallRecord] = deque(maxlen=max_history)

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Вернуть immutable capabilities без обращения к environment/backend."""
        return self._capabilities

    @property
    def calls(self) -> tuple[LLMCallRecord, ...]:
        """Вернуть bounded snapshot истории завершившихся попыток."""
        return tuple(self._history)

    @property
    def call_count(self) -> int:
        """Вернуть число зарезервированных attempts, включая отменённые."""
        return self._offset

    @property
    def remaining_steps(self) -> int:
        """Вернуть число ещё не использованных script steps."""
        return len(self._script) - self._offset

    def _request(self, request: LLMRequest) -> LLMRequest:
        if type(request) is not LLMRequest:
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
        try:
            checked = LLMRequest.model_validate(request.model_dump(warnings="error"))
        except (ValueError, TypeError, RecursionError):
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID) from None
        if checked.prompt is None:
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
        if checked.purpose not in self.capabilities.supported_purposes:
            raise LLMProviderError(LLMErrorCode.CAPABILITY_MISMATCH)
        if len(checked.payload_json.encode()) > self.capabilities.max_input_bytes:
            raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
        return checked

    def _response(
        self, request: LLMRequest, step: ScriptedResponse, at: datetime
    ) -> LLMResponse:
        payload = step.output_json
        try:
            output_bytes = payload.encode()
        except UnicodeError:
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE) from None
        if len(output_bytes) > min(
            request.max_output_bytes, self.capabilities.max_output_bytes
        ):
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE)
        limits = self.capabilities
        if (
            (
                limits.max_input_tokens is not None
                and step.input_tokens > limits.max_input_tokens
            )
            or (
                limits.max_output_tokens is not None
                and step.output_tokens > limits.max_output_tokens
            )
            or (
                limits.context_window_tokens is not None
                and step.input_tokens + step.output_tokens
                > limits.context_window_tokens
            )
        ):
            raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
        try:
            return LLMResponse(
                request_id=request.request_id,
                provider_id=limits.provider_id,
                provider_version=limits.provider_version,
                model_id=limits.model_id,
                response_schema_id=request.response_schema_id,
                response_schema_version=request.response_schema_version,
                output_json=payload,
                prompt_fingerprint=request.prompt_fingerprint,
                prompt=request.prompt,
                generation_fingerprint="sha256:"
                + hashlib.sha256(output_bytes).hexdigest(),
                finish_reason="stop",
                input_tokens=step.input_tokens,
                output_tokens=step.output_tokens,
                generated_at=at,
            )
        except (ValueError, TypeError, RecursionError):
            raise LLMProviderError(LLMErrorCode.INVALID_RESPONSE) from None

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Проверить запрос, выполнить один step и сохранить безопасную историю.

        Cancellation распространяется после записи metadata. Ошибки до начала
        попытки не расходуют step; elapsed_ms — simulated latency из fixture.
        """
        request = self._request(request)
        if self._offset == len(self._script):
            raise LLMProviderError(LLMErrorCode.SCRIPT_EXHAUSTED)
        step = self._script[self._offset]
        request_hash = canonical_sha256_value(request)
        if (
            step.expected_request_fingerprint is not None
            and step.expected_request_fingerprint != request_hash
        ):
            raise LLMProviderError(LLMErrorCode.SCRIPT_MISMATCH)
        started = TypeAdapter(UtcDateTime).validate_python(self._clock())
        finished = started + timedelta(milliseconds=step.elapsed_ms)
        self._offset += 1
        attempt = self._offset
        prompt = request.prompt
        assert prompt is not None

        def record(
            outcome: Literal["success", "failed", "cancelled"],
            *,
            code: LLMErrorCode | None = None,
            response: LLMResponse | None = None,
        ) -> LLMCallRecord:
            call = LLMCallRecord(
                attempt=attempt,
                request_fingerprint=request_hash,
                capabilities_fingerprint=canonical_sha256_value(self.capabilities),
                prompt=prompt,
                outcome=outcome,
                error_code=code,
                generation_fingerprint=response.generation_fingerprint
                if response
                else None,
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                elapsed_ms=step.elapsed_ms,
                started_at=started,
                finished_at=finished,
            )
            self._history.append(call)
            return call

        try:
            if self._checkpoint is not None:
                await self._checkpoint()
            if isinstance(step, ScriptedFailure):
                raise LLMProviderError(step.code)
            response = self._response(request, step, finished)
        except asyncio.CancelledError:
            record("cancelled")
            raise
        except LLMProviderError as error:
            call = record("failed", code=LLMErrorCode(error.error_code))
            raise LLMProviderError(LLMErrorCode(error.error_code), call=call) from None
        record("success", response=response)
        return response
