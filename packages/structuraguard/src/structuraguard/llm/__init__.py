"""LLM adapters и policy-aware router; optional HTTPX загружается при start."""

from structuraguard.llm._structured import LLMPromptTemplate, LLMResponseSchema
from structuraguard.llm.fake import FakeLLMProvider, ScriptedFailure, ScriptedResponse
from structuraguard.llm.no_llm import NoLLMProvider
from structuraguard.llm.openai_compatible import (
    OpenAICompatibleConfig,
    OpenAICompatibleHeader,
    OpenAICompatibleProvider,
)
from structuraguard.llm.router import PolicyAwareLLMRouter

__all__ = [
    "FakeLLMProvider",
    "LLMPromptTemplate",
    "LLMResponseSchema",
    "NoLLMProvider",
    "OpenAICompatibleConfig",
    "OpenAICompatibleHeader",
    "OpenAICompatibleProvider",
    "PolicyAwareLLMRouter",
    "ScriptedFailure",
    "ScriptedResponse",
]
