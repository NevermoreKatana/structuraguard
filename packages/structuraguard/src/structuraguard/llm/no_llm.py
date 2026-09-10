"""Явный adapter deterministic-only режима без generation и побочных эффектов."""

from structuraguard.contracts.llm import LLMErrorCode, LLMExecutionEnvironment
from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)
from structuraguard.exceptions import LLMProviderError


class NoLLMProvider:
    """Обозначает запрет LLM: caller выбирает deterministic flow по capabilities.

    Прямой вызов всегда даёт LLM_POLICY_DENIED. Adapter не создаёт фиктивный
    успешный ответ, не читает request payload и не запускает fallback.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Вернуть disabled capabilities без доступного generation purpose."""
        return ProviderCapabilities(
            provider_id="no_llm",
            provider_version="1.0.0",
            model_id="none",
            structured_output=False,
            supported_purposes=(),
            max_input_bytes=0,
            max_output_bytes=0,
            execution_environment=LLMExecutionEnvironment.DISABLED,
        )

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Отклонить любую попытку generation типизированной LLM_POLICY_DENIED."""
        raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
