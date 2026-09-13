"""Общий дополнительный literal veto M6/M10; не grammar и не sandbox."""

import re

ACTIVE_CONTENT = r"(?is)(```|<script\b|\$\(|\b(?:eval|exec|compile|__import__|os\.system|subprocess\.\w+)\s*\(|\b(?:select\b.{0,256}?\bfrom|insert\s+into|delete\s+from|(?:drop|alter|create|truncate)\s+(?:table|database)|update\s+\w+\s+set)\b|\b(?:import\s+(?:os|sys|subprocess)|from\s+\w+\s+import|def\s+\w+\s*\(|(?:curl|wget|bash|powershell|cmd\.exe)\s+|rm\s+-|python[0-9.]*\s+-c)|ignore\s+(?:all\s+)?(?:previous|system)\s+instructions|игнорируй\s+(?:все\s+)?(?:предыдущие|системные)\s+инструкции)"
ACTIVE_CONTENT_PATTERN = re.compile(ACTIVE_CONTENT)
