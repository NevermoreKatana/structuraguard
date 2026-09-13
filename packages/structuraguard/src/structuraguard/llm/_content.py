"""Общий дополнительный veto для недоверенного LLM content; не sandbox."""

import json

from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.domain.llm_content import ACTIVE_CONTENT_PATTERN
from structuraguard.exceptions import LLMProviderError


def reject_active_content(text: str) -> None:
    """Консервативный veto известных code/command/injection fragments.

    Главная защита — закрытая grammar и source membership: строки никогда не
    получают execution capability. Regex — дополнительный veto, не sandbox.
    """
    try:
        pending: list[object] = [json.loads(text)]
    except (ValueError, RecursionError):
        pending = [text]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and ACTIVE_CONTENT_PATTERN.search(value):
            raise LLMProviderError(LLMErrorCode.UNSAFE_CONTENT)
