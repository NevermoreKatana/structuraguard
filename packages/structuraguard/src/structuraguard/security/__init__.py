"""Security exports загружаются явно; импорт submodule не активирует все adapters."""

from importlib import import_module as _import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from structuraguard.contracts.audit import (
        AuditEnvelope,
        AuditHead,
        AuditKind,
        SecurityAuditEvent,
    )
    from structuraguard.contracts.common import DataClassification
    from structuraguard.contracts.database_policy import (
        DatabasePolicy,
        DatabaseTableRule,
        PostgreSQLAuditPolicy,
    )
    from structuraguard.contracts.injection import (
        InjectionAction,
        InjectionCode,
        InjectionLocation,
        InjectionOrigin,
        InjectionPolicy,
        InjectionReport,
        InjectionSeverity,
        InjectionSignal,
        InjectionSummary,
    )
    from structuraguard.contracts.privacy import (
        CategoryRule,
        CustomPattern,
        DetectionPolicy,
        DetectionReport,
        PlaceholderHandle,
        PlaceholderMapPolicy,
        PrivacySummary,
        ScanLimits,
        SensitiveCategory,
    )
    from structuraguard.contracts.security import (
        Resource,
        ResourceAuditEvent,
        SecurityLimits,
        SecurityPolicy,
    )
    from structuraguard.security.audit import (
        AuditChain,
        HMACAuditSigner,
        MemoryAuditKeys,
    )
    from structuraguard.security.audit_store import MemoryAuditChainStore
    from structuraguard.security.classification import ContentProtector
    from structuraguard.security.placeholder_store import (
        EncryptedMemoryPlaceholderStore,
    )
    from structuraguard.security.redaction import (
        RedactedField,
        RedactionResult,
        restore_fields,
        restore_text,
    )
    from structuraguard.security.scanner import InjectionAwareSecurityScanner
    from structuraguard.security.session import SecuritySession
    from structuraguard.security.signals import InjectionDetector


__all__ = (
    "AuditChain",
    "AuditEnvelope",
    "AuditHead",
    "AuditKind",
    "CategoryRule",
    "ContentProtector",
    "CustomPattern",
    "DataClassification",
    "DatabasePolicy",
    "DatabaseTableRule",
    "DetectionPolicy",
    "DetectionReport",
    "EncryptedMemoryPlaceholderStore",
    "HMACAuditSigner",
    "InjectionAction",
    "InjectionAwareSecurityScanner",
    "InjectionCode",
    "InjectionDetector",
    "InjectionLocation",
    "InjectionOrigin",
    "InjectionPolicy",
    "InjectionReport",
    "InjectionSeverity",
    "InjectionSignal",
    "InjectionSummary",
    "MemoryAuditChainStore",
    "MemoryAuditKeys",
    "PlaceholderHandle",
    "PlaceholderMapPolicy",
    "PostgreSQLAuditPolicy",
    "PrivacySummary",
    "RedactedField",
    "RedactionResult",
    "Resource",
    "ResourceAuditEvent",
    "ScanLimits",
    "SecurityAuditEvent",
    "SecurityLimits",
    "SecurityPolicy",
    "SecuritySession",
    "SensitiveCategory",
    "restore_fields",
    "restore_text",
)

_EXPORT_GROUPS = (
    (
        "structuraguard.contracts.audit",
        ("AuditEnvelope", "AuditHead", "AuditKind", "SecurityAuditEvent"),
    ),
    ("structuraguard.contracts.common", ("DataClassification",)),
    (
        "structuraguard.contracts.database_policy",
        ("DatabasePolicy", "DatabaseTableRule", "PostgreSQLAuditPolicy"),
    ),
    (
        "structuraguard.contracts.injection",
        (
            "InjectionAction",
            "InjectionCode",
            "InjectionLocation",
            "InjectionOrigin",
            "InjectionPolicy",
            "InjectionReport",
            "InjectionSeverity",
            "InjectionSignal",
            "InjectionSummary",
        ),
    ),
    (
        "structuraguard.contracts.privacy",
        (
            "CategoryRule",
            "CustomPattern",
            "DetectionPolicy",
            "DetectionReport",
            "PlaceholderHandle",
            "PlaceholderMapPolicy",
            "PrivacySummary",
            "ScanLimits",
            "SensitiveCategory",
        ),
    ),
    (
        "structuraguard.contracts.security",
        ("Resource", "ResourceAuditEvent", "SecurityLimits", "SecurityPolicy"),
    ),
    ("structuraguard.security.classification", ("ContentProtector",)),
    ("structuraguard.security.placeholder_store", ("EncryptedMemoryPlaceholderStore",)),
    (
        "structuraguard.security.redaction",
        ("RedactedField", "RedactionResult", "restore_fields", "restore_text"),
    ),
    ("structuraguard.security.scanner", ("InjectionAwareSecurityScanner",)),
    ("structuraguard.security.session", ("SecuritySession",)),
    ("structuraguard.security.signals", ("InjectionDetector",)),
    (
        "structuraguard.security.audit",
        ("AuditChain", "HMACAuditSigner", "MemoryAuditKeys"),
    ),
    ("structuraguard.security.audit_store", ("MemoryAuditChainStore",)),
)


def __getattr__(name: str) -> object:
    """Сохранить публичный API без eager DLP/crypto imports для DB/runner ports."""
    for module, names in _EXPORT_GROUPS:
        if name in names:
            return getattr(_import_module(module), name)
    raise AttributeError("Неизвестный security export")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
