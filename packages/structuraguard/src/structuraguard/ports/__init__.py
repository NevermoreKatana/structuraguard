"""Минимальные adapter protocols milestone M2."""

from structuraguard.ports.database import DatabaseAdapter
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.mapping import CandidateMapper
from structuraguard.ports.parser import Parser
from structuraguard.ports.profiling import NormalizedDataProfiler
from structuraguard.ports.security import PIIClassifier, SecurityScanner
from structuraguard.ports.semantic import (
    ParsePlanExecutor,
    ParsePlanValidator,
    SemanticStructureAnalyzer,
    StructuralProfiler,
)
from structuraguard.ports.stores import AuditStore, StagingStore

__all__ = (
    "AuditStore",
    "CandidateMapper",
    "DatabaseAdapter",
    "LLMProvider",
    "NormalizedDataProfiler",
    "PIIClassifier",
    "ParsePlanExecutor",
    "ParsePlanValidator",
    "Parser",
    "SecurityScanner",
    "SemanticStructureAnalyzer",
    "StagingStore",
    "StructuralProfiler",
)
