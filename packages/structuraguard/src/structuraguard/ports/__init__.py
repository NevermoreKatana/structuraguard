"""Минимальные adapter protocols milestone M2."""

from structuraguard.ports.database import DatabaseAdapter
from structuraguard.ports.json_schema import JsonSchemaValidator
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.mapping import CandidateMapper, MappingPlanValidator
from structuraguard.ports.normalization import Normalizer
from structuraguard.ports.parser import Parser
from structuraguard.ports.profiling import NormalizedDataProfiler
from structuraguard.ports.provenance import ProvenanceValidator
from structuraguard.ports.security import PIIClassifier, SecurityScanner
from structuraguard.ports.semantic import (
    ParsePlanExecutor,
    ParsePlanValidator,
    SemanticStructureAnalyzer,
    StructuralProfiler,
)
from structuraguard.ports.stores import AuditStore, StagingStore
from structuraguard.ports.validation import ConstraintReader

__all__ = (
    "AuditStore",
    "CandidateMapper",
    "ConstraintReader",
    "DatabaseAdapter",
    "JsonSchemaValidator",
    "LLMProvider",
    "MappingPlanValidator",
    "NormalizedDataProfiler",
    "Normalizer",
    "PIIClassifier",
    "ParsePlanExecutor",
    "ParsePlanValidator",
    "Parser",
    "ProvenanceValidator",
    "SecurityScanner",
    "SemanticStructureAnalyzer",
    "StagingStore",
    "StructuralProfiler",
)
