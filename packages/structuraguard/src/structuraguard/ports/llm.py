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
    """Возвращает только bounded structured output без tools и DB access."""

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Вернуть проверяемые возможности выбранной provider-конфигурации."""

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        """Сформировать structured output заданной response schema.

        Args:
            request: Bounded canonical payload с ``SecurityApproval``.

        Returns:
            Недоверенный output с identity фактического provider и model.

        Side effects:
            Provider I/O принадлежит адаптеру; tools, source/DB handles и
            credentials контрактом не передаются.
        """
