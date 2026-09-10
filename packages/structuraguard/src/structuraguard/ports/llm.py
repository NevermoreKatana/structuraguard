"""Provider-neutral port строго структурированного LLM-вызова."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from structuraguard.contracts.reports import (
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
)


@runtime_checkable
class LLMProvider(Protocol):
    """Возвращает bounded structured output без tools и DB access.

    Capabilities типизированы; disabled provider всегда отклоняет generation
    через LLM_POLICY_DENIED. Новый caller передаёт LLMPrompt с version/hash.
    Schema capability и применение semantic schema — разные проверки:
    structured JSON сам по себе не подтверждает ParsePlan или provenance.
    """

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Вернуть проверяемые возможности выбранной provider-конфигурации."""

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Сформировать structured output заданной response schema.

        Args:
            request: Bounded canonical payload с ``SecurityApproval``.

        Returns:
            Недоверенный output с identity фактического provider и model.

        Raises:
            LLMProviderError: Нормализованный code без backend-specific details.
            asyncio.CancelledError: Caller cancellation без скрытого retry.

        Side effects:
            Provider I/O принадлежит адаптеру; tools, source/DB handles и
            credentials контрактом не передаются.
            Adapter повторно проверяет входной DTO и SecurityApproval; request,
            response schema и prompt identity должны совпасть в результате.
            Retry/fallback принадлежат caller/router, а не этому port.
        """
