from __future__ import annotations

import inspect
from collections.abc import AsyncIterable, AsyncIterator
from typing import get_origin, get_type_hints

import pytest

from structuraguard.contracts.database import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    LoadContext,
    StagingContext,
)
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
    StructureAnalysisRequest,
    StructureAnalysisResult,
    ValidatedParsePlan,
)
from structuraguard.contracts.reports import (
    AuditEvent,
    LLMRequest,
    LLMResponse,
    LoadReport,
    ProviderCapabilities,
    SecurityReport,
    SecurityScanRequest,
)
from structuraguard.contracts.source import ExtractedBatch, ProbeResult, SourceArtifact
from structuraguard.ports import (
    AuditStore,
    DatabaseAdapter,
    LLMProvider,
    ParsePlanExecutor,
    ParsePlanValidator,
    Parser,
    SecurityScanner,
    SemanticStructureAnalyzer,
    StagingStore,
)
from structuraguard.ports.source import ParseContext, ProbeContext


class _TypedParser:
    adapter_id = "fake.parser"
    version = "1.0.0"
    priority = 10

    async def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        raise NotImplementedError

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[ExtractedBatch]:
        return self._batches()

    async def _batches(self) -> AsyncIterator[ExtractedBatch]:
        batches: tuple[ExtractedBatch, ...] = ()
        for batch in batches:
            yield batch


class _TypedAnalyzer:
    async def analyze(
        self, request: StructureAnalysisRequest
    ) -> StructureAnalysisResult:
        raise NotImplementedError


class _TypedParsePlanValidator:
    def validate(
        self, request: ParsePlanValidationRequest
    ) -> ParsePlanValidationResult:
        raise NotImplementedError


class _TypedParsePlanExecutor:
    def execute(
        self,
        batches: AsyncIterable[ExtractedBatch],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncIterator[NormalizedBatch]:
        return self._batches(batches)

    async def _batches(
        self, batches: AsyncIterable[ExtractedBatch]
    ) -> AsyncIterator[NormalizedBatch]:
        async for _batch in batches:
            normalized: tuple[NormalizedBatch, ...] = ()
            for item in normalized:
                yield item


class _TypedDatabaseAdapter:
    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        raise NotImplementedError

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: ValidatedMappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        raise NotImplementedError


class _TypedLLMProvider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        raise NotImplementedError

    async def generate_structured(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError


class _TypedSecurityScanner:
    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        raise NotImplementedError


class _TypedStagingStore:
    async def stage(self, batch: NormalizedBatch, context: StagingContext) -> None:
        raise NotImplementedError


class _TypedAuditStore:
    async def append(self, event: AuditEvent) -> None:
        raise NotImplementedError


_PARSER: Parser = _TypedParser()
_ANALYZER: SemanticStructureAnalyzer = _TypedAnalyzer()
_PARSE_VALIDATOR: ParsePlanValidator = _TypedParsePlanValidator()
_PARSE_EXECUTOR: ParsePlanExecutor = _TypedParsePlanExecutor()
_DATABASE: DatabaseAdapter = _TypedDatabaseAdapter()
_LLM: LLMProvider = _TypedLLMProvider()
_SECURITY: SecurityScanner = _TypedSecurityScanner()
_STAGING: StagingStore = _TypedStagingStore()
_AUDIT: AuditStore = _TypedAuditStore()


def test_typed_fakes_satisfy_runtime_protocols() -> None:
    assert isinstance(_PARSER, Parser)
    assert isinstance(_ANALYZER, SemanticStructureAnalyzer)
    assert isinstance(_PARSE_VALIDATOR, ParsePlanValidator)
    assert isinstance(_PARSE_EXECUTOR, ParsePlanExecutor)
    assert isinstance(_DATABASE, DatabaseAdapter)
    assert isinstance(_LLM, LLMProvider)
    assert isinstance(_SECURITY, SecurityScanner)
    assert isinstance(_STAGING, StagingStore)
    assert isinstance(_AUDIT, AuditStore)


@pytest.mark.parametrize(
    ("adapter", "method_name", "is_coroutine", "is_streaming"),
    (
        (_PARSER, "probe", True, False),
        (_PARSER, "parse", False, True),
        (_ANALYZER, "analyze", True, False),
        (_PARSE_VALIDATOR, "validate", False, False),
        (_PARSE_EXECUTOR, "execute", False, True),
        (_DATABASE, "inspect", True, False),
        (_DATABASE, "execute", True, False),
        (_LLM, "generate_structured", True, False),
        (_SECURITY, "scan", True, False),
        (_STAGING, "stage", True, False),
        (_AUDIT, "append", True, False),
    ),
)
def test_typed_fake_call_shapes_match_the_protocol_contract(
    adapter: object,
    method_name: str,
    is_coroutine: bool,
    is_streaming: bool,
) -> None:
    method = getattr(adapter, method_name)
    return_annotation = get_type_hints(method)["return"]

    assert inspect.iscoroutinefunction(method) is is_coroutine
    assert (get_origin(return_annotation) is AsyncIterator) is is_streaming
