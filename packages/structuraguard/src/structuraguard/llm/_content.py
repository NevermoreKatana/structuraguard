"""Общий дополнительный veto для недоверенного LLM content; не sandbox."""

import json
import re

from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.exceptions import LLMProviderError


def reject_active_content(text: str) -> None:
    """Консервативный veto известных code/command/injection fragments.

    Главная защита — закрытая grammar и source membership: строки никогда не
    получают execution capability. Regex — дополнительный veto, не sandbox.
    """
    pattern = r"(?is)(```|<script\b|\$\(|\b(?:eval|exec|compile|__import__|os\.system|subprocess\.\w+)\s*\(|\b(?:select\b.{0,256}?\bfrom|insert\s+into|delete\s+from|(?:drop|alter|create|truncate)\s+(?:table|database)|update\s+\w+\s+set)\b|\b(?:import\s+(?:os|sys|subprocess)|from\s+\w+\s+import|def\s+\w+\s*\(|(?:curl|wget|bash|powershell|cmd\.exe)\s+|rm\s+-|python[0-9.]*\s+-c)|ignore\s+(?:all\s+)?(?:previous|system)\s+instructions|игнорируй\s+(?:все\s+)?(?:предыдущие|системные)\s+инструкции)"
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
        elif isinstance(value, str) and re.search(pattern, value):
            raise LLMProviderError(LLMErrorCode.UNSAFE_CONTENT)
