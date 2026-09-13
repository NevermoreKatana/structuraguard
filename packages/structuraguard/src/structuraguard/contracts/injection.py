"""Heuristic signals с закрытой severity, original locations и safe summaries."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, StrictInt, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .common import FingerprintStr
from .privacy import ScanLimits


class InjectionSeverity(StrEnum):
    """Закрытые уровни heuristic риска; NONE не означает безопасность."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class InjectionAction(StrEnum):
    """Дополнительные ограничения маршрута/решения; OBSERVE не выдаёт approval."""

    OBSERVE = "observe"
    LOCAL_ONLY = "local_only"
    NEEDS_REVIEW = "needs_review"
    BLOCK = "block"


class InjectionCode(StrEnum):
    """Закрытые причины сигналов без raw excerpts и исполняемых команд."""

    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_SPOOFING = "role_spoofing"
    SECRET_EXFILTRATION = "secret_exfiltration"
    TOOL_AUTHORITY = "tool_authority"
    CONCEALMENT = "concealment"
    ENCODED_INSTRUCTION = "encoded_instruction"
    ACTIVE_CONTENT = "active_content"


class InjectionOrigin(StrEnum):
    """Происхождение untrusted текста; значение enum не выдаёт authority."""

    SOURCE_TEXT = "source_text"
    DB_METADATA = "db_metadata"
    TEMPLATE = "template"
    LLM_PAYLOAD = "llm_payload"
    LLM_OUTPUT = "llm_output"


class InjectionPolicy(FrozenContract):
    """Дополнительный control; отсутствие сигналов не выдаёт egress approval."""

    limits: ScanLimits = Field(
        default_factory=lambda: ScanLimits(
            max_fields=512, max_findings=128, max_work=1_000_000_000
        )
    )
    high_risk_action: InjectionAction = InjectionAction.NEEDS_REVIEW
    max_normalized_chars: Annotated[StrictInt, Field(gt=0, le=4_000_000)] = 262144
    max_json_depth: Annotated[StrictInt, Field(gt=0, le=64)] = 32
    max_json_items: Annotated[StrictInt, Field(gt=0, le=65536)] = 4096
    max_run_scans: Annotated[StrictInt, Field(gt=0, le=1024)] = 128
    max_run_signals: Annotated[StrictInt, Field(gt=0, le=256)] = 128

    @model_validator(mode="after")
    def no_unrestricted_high_risk(self) -> Self:
        """Отклонить high-risk OBSERVE через ValueError без выполнения scan."""
        if self.high_risk_action is InjectionAction.OBSERVE:
            raise ValueError("High risk требует ограничения")
        return self

    @property
    def fingerprint(self) -> str:
        """Вернуть SHA-256 policy без input; hash не является authentication."""
        return canonical_sha256_value(self)

    def action_for(self, severity: InjectionSeverity) -> InjectionAction:
        """Вернуть действие для severity; critical всегда BLOCK, medium LOCAL_ONLY."""
        if severity is InjectionSeverity.CRITICAL:
            return InjectionAction.BLOCK
        if severity is InjectionSeverity.HIGH:
            return self.high_risk_action
        if severity is InjectionSeverity.MEDIUM:
            return InjectionAction.LOCAL_ONLY
        return InjectionAction.OBSERVE


class InjectionLocation(FrozenContract):
    """Offsets в исходной строке, а не в нормализованной копии; без raw path/label."""

    origin: InjectionOrigin
    scan_index: Annotated[StrictInt, Field(ge=0, le=1024)] = 0
    item_index: Annotated[StrictInt, Field(ge=0, le=65536)]
    part: Literal["text", "key", "value"]
    start: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    end: Annotated[StrictInt, Field(gt=0, le=1_000_000)]

    @model_validator(mode="after")
    def span(self) -> Self:
        """Проверить непустой полуинтервал Unicode offsets; иначе ValueError."""
        if self.start >= self.end:
            raise ValueError("Требуется непустой evidence span")
        return self


class InjectionSignal(FrozenContract):
    """Неизменяемые code/severity/location без raw текста и разрешений egress."""

    code: InjectionCode
    severity: InjectionSeverity
    location: InjectionLocation


class InjectionSummary(FrozenContract):
    """Закрытые metadata для events/logs, без excerpts, locations и input hashes."""

    action: InjectionAction
    severity: InjectionSeverity
    signal_count: Annotated[StrictInt, Field(ge=0, le=256)]
    codes: tuple[InjectionCode, ...]
    untrusted: Literal[True] = True
    complete: Literal[True] = True
    coverage: Literal["heuristic_not_exhaustive"] = "heuristic_not_exhaustive"


class InjectionReport(FrozenContract):
    """Scan evidence не является SecurityApproval или доказательством безопасности."""

    policy_fingerprint: FingerprintStr
    payload_fingerprint: FingerprintStr
    signals: tuple[InjectionSignal, ...] = Field(max_length=256)
    severity: InjectionSeverity
    action: InjectionAction
    scanned_chars: Annotated[StrictInt, Field(ge=0, le=1_000_000)]
    work_used: Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
    untrusted: Literal[True] = True
    complete: Literal[True] = True
    coverage: Literal["heuristic_not_exhaustive"] = "heuristic_not_exhaustive"

    @model_validator(mode="after")
    def risk(self) -> Self:
        """Согласовать severity/action с signals; противоречие даёт ValueError."""
        levels = list(InjectionSeverity)
        severity = max(
            (s.severity for s in self.signals),
            key=levels.index,
            default=InjectionSeverity.NONE,
        )
        if self.severity is not severity:
            raise ValueError("Severity не соответствует signals")
        if (
            severity is InjectionSeverity.CRITICAL
            and self.action is not InjectionAction.BLOCK
        ):
            raise ValueError("Critical evidence требует block")
        if (
            severity is InjectionSeverity.HIGH
            and self.action is InjectionAction.OBSERVE
        ):
            raise ValueError("High evidence требует ограничения")
        if (
            severity is InjectionSeverity.MEDIUM
            and self.action is InjectionAction.OBSERVE
        ):
            raise ValueError("Medium evidence требует ограничения")
        return self

    def safe_summary(self) -> InjectionSummary:
        """Вернуть counts/codes/action без locations, input hashes и excerpts."""
        return InjectionSummary(
            action=self.action,
            severity=self.severity,
            signal_count=len(self.signals),
            codes=tuple(
                code
                for code in InjectionCode
                if any(s.code is code for s in self.signals)
            ),
        )
