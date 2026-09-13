"""Конечные multilingual patterns; недоверенный input не задаёт regex."""

import re
from dataclasses import dataclass

from structuraguard.contracts.injection import InjectionCode as Code
from structuraguard.contracts.injection import InjectionSeverity as Severity
from structuraguard.domain.llm_content import ACTIVE_CONTENT_PATTERN


@dataclass(frozen=True, slots=True)
class SignalPattern:
    code: Code
    severity: Severity
    regex: re.Pattern[str]
    cost: int = 512


_PATTERNS = (
    (
        Code.INSTRUCTION_OVERRIDE,
        Severity.HIGH,
        r"\b(?:ignore|disregard|forget|override)\s{1,8}(?:(?:all|the|your)\s{1,8})?(?:previous|prior|system|above)\s{1,8}(?:instructions|rules|prompts)\b"
        r"|игнорируй\s{1,8}(?:все\s{1,8})?(?:предыдущие|системные|прежние)\s{1,8}инструкции"
        r"|(?:забудь|отмени)\s{1,8}(?:все\s{1,8})?(?:правила|инструкции)"
        r"|ignora\s{1,8}(?:las\s{1,8})?instrucciones\s{1,8}anteriores"
        r"|ignorez\s{1,8}(?:les\s{1,8})?instructions\s{1,8}précédentes"
        r"|ignoriere\s{1,8}(?:alle\s{1,8})?vorherigen\s{1,8}anweisungen"
        r"|忽略(?:之前|以前|先前|所有)(?:的)?(?:指令|说明|指示)"
        r"|تجاهل\s{1,8}التعليمات\s{1,8}السابقة",
    ),
    (
        Code.ROLE_SPOOFING,
        Severity.HIGH,
        r"(?:\[|<)\|?(?:im_start|start_header_id|system|developer)\|?(?:\]|>)"
        r"|\[/?inst\]|</?(?:system|developer)>|\byou are now\s{1,8}(?:the\s{1,8})?(?:administrator|system|developer)\b"
        r"|теперь ты\s{1,8}(?:системный|администратор|разработчик)",
    ),
    (
        Code.SECRET_EXFILTRATION,
        Severity.CRITICAL,
        r"\b(?:send|reveal|print|exfiltrate|return|upload)\b.{0,64}\b(?:password|secret|api[ _-]?key|access[ _-]?token|credentials|system prompt)\b"
        r"|(?:отправь|раскрой|верни|покажи|выведи).{0,64}(?:парол|секрет|токен|ключ api|системн.{0,12}промпт)"
        r"|(?:envía|revela).{0,64}(?:contraseña|clave|secreto)"
        r"|(?:泄露|发送|输出).{0,32}(?:密码|密钥|令牌)",
    ),
    (
        Code.TOOL_AUTHORITY,
        Severity.HIGH,
        r"\b(?:call|invoke|use|enable)\b.{0,48}\b(?:tool|shell|terminal|database connection)\b"
        r"|(?:вызови|используй|запусти).{0,48}(?:инструмент|терминал|оболочку)"
        r"|подключись\s{1,8}к\s{1,8}базе\s{1,8}данных"
        r"|(?:调用|执行).{0,32}(?:工具|终端)",
    ),
    (
        Code.CONCEALMENT,
        Severity.MEDIUM,
        r"\b(?:do not|don't|never)\s{1,8}(?:mention|reveal|tell)\b.{0,48}\b(?:instructions|user|request)\b"
        r"|(?:не сообщай|не говори|скрой).{0,48}(?:пользовател|инструкц)",
    ),
    (
        Code.ENCODED_INSTRUCTION,
        Severity.HIGH,
        r"\bdecode\b.{0,48}\b(?:base64|hex|rot13)\b.{0,48}\b(?:execute|obey|follow|run)\b"
        r"|(?:декодируй|расшифруй).{0,64}(?:выполни|исполняй|запусти)",
    ),
)

PATTERNS = (
    *(
        SignalPattern(code, severity, re.compile(pattern, re.DOTALL))
        for code, severity, pattern in _PATTERNS
    ),
    SignalPattern(Code.ACTIVE_CONTENT, Severity.HIGH, ACTIVE_CONTENT_PATTERN, 1024),
)
