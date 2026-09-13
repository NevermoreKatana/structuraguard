"""NO_LLM и явный scanner deny запрещают egress на SDK boundary."""

from dataclasses import replace
from decimal import Decimal

import pytest
from tests.fakes.llm import fixed_clock
from tests.fakes.pipeline import FakeDatabase, defaults, engine, request
from tests.fakes.semantic import Scanner
from tests.unit.pipeline.test_llm import deps

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts import IssueSeverity, SemanticParsingMode, ValidationIssue
from structuraguard.contracts import PipelineStatus as S
from structuraguard.contracts.deterministic_mapping import MappingWeights
from structuraguard.contracts.llm import LLMBudget, LLMRoutingMode, LLMRoutingPolicy
from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.llm import FakeLLMProvider
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SourceRequest


class DeniedScanner(Scanner):
    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        allowed = await super().scan(request)
        return SecurityReport.model_validate(
            {
                **allowed.model_dump(mode="python"),
                "decision": "blocked",
                "status": S.REJECTED_SECURITY,
                "blocked_items": 1,
                "issues": (
                    ValidationIssue(
                        code="LLM_DATA_ROUTING_FORBIDDEN",
                        message_key="LLM_DATA_ROUTING_FORBIDDEN",
                        severity=IssueSeverity.ERROR,
                    ),
                ),
            }
        )


@pytest.mark.anyio
async def test_scanner_deny_precedes_external_parsing_provider() -> None:
    from tests.fakes.pipeline import Stream

    provider = FakeLLMProvider((), clock=fixed_clock)
    scanner = DeniedScanner()
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=replace(
            deps(provider, SemanticParsingMode.LLM_FIRST),
            scanner=scanner,
        ),
    )
    result = await sdk.ingest(
        SourceRequest(
            stream=Stream(b"name,n\nAda,1\nBob,2\n"),
            display_name="input.csv",
        )
    )
    assert result.status in {S.REJECTED_SECURITY, S.NEEDS_REVIEW}
    assert len(scanner.requests) == 1 and provider.call_count == 0
    assert result.security_report.scans
    assert result.security_report.scans[-1].decision == "blocked"
    assert result.database_report is None and result.load_report is None


@pytest.mark.anyio
async def test_no_llm_applies_to_unresolved_database_mapping() -> None:
    db = FakeDatabase()
    provider = FakeLLMProvider((), clock=fixed_clock)
    original = defaults(db)
    configured = replace(
        original,
        providers=(provider,),
        scanner=Scanner(),
        parsing=ParsingPolicy(mode=SemanticParsingMode.LLM_ASSISTED),
        routing=LLMRoutingPolicy(
            policy_id="offline",
            mode=LLMRoutingMode.NO_LLM,
            routes=(),
            budget=LLMBudget(max_calls=1, max_tokens=1000, max_time_ms=30000),
        ),
        ranking=original.ranking.model_copy(
            update={
                "auto_threshold": Decimal(1),
                "weights": MappingWeights(
                    name_similarity=Decimal("0.95"),
                    alias_match=Decimal("0.05"),
                    type_compatibility=Decimal(0),
                    value_pattern_match=Decimal(0),
                    structural_context=Decimal(0),
                    database_relation_score=Decimal(0),
                ),
            }
        ),
    )
    result = await engine(dependencies=configured).ingest(request())
    assert result.status is S.NEEDS_REVIEW
    assert result.parse_plan and result.database_report and result.candidates
    assert result.mapping_plan is None and result.semantic_mapping is None
    assert result.errors[-1].code == "SDK_MAPPING_NEEDS_REVIEW"
    assert provider.call_count == db.writes == 0
    assert not result.provider_metadata and not db.artifacts
