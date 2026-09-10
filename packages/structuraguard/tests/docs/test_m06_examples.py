"""Публичные примеры и русские docstring M6 проверяются без внешней сети."""

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from structuraguard import llm, parsing, structure
from structuraguard.contracts import (
    DocumentEntityProposal,
    DocumentEntitySuggestion,
    DocumentFieldProposal,
    DocumentSpanGrouping,
    DocumentSpanSelector,
    LLMAnalysisContext,
    LLMCallRecord,
    LLMPlanProvenance,
    LLMPrompt,
    LLMRequest,
    LLMResponse,
    LLMStructurePolicy,
    LLMStructureSuggestion,
    ProviderCapabilities,
    QuotedSpan,
    SemanticEntityProposal,
    SemanticFieldProposal,
    SemanticParseReport,
    SemanticPathStep,
    SemanticPlanProposal,
    SemanticSelector,
    SourceTextSpan,
)
from structuraguard.ports import LLMProvider


def test_m06_provider_example_runs_offline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs" / "llm.md").read_text()
    match = re.search(
        r"<!-- example:m06-provider:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    guard = (
        "import sys\n"
        "def no_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Network запрещена в примере M6')\n"
        "sys.addaudithook(no_network)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", guard + match.group("code")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == '{"fields":[]}\n1 call; prompt 1.0.0\n'


def test_m06_session_example_runs_offline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    match = re.search(
        r"<!-- example:m06-session:start -->\s*```python\n(?P<code>.*?)\n```",
        (root / "docs" / "semantic-parsing.md").read_text(),
        flags=re.DOTALL,
    )
    assert match is not None
    guard = (
        "import sys\n"
        "def no_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Network запрещена в примере M6')\n"
        "sys.addaudithook(no_network)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", guard + match.group("code")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout == "semantic session: 2 records; 0 LLM calls\n"


@pytest.mark.parametrize(
    "public_object",
    [
        *(getattr(llm, name) for name in llm.__all__),
        *(getattr(parsing, name) for name in parsing.__all__),
        LLMProvider,
        ProviderCapabilities,
        LLMRequest,
        LLMResponse,
        LLMPrompt,
        LLMCallRecord,
        structure.LLMStructureAnalyzer,
        structure.semantic_prompt,
        structure.semantic_response_schema,
        structure.HybridAnalysis,
        structure.document_prompt,
        structure.document_response_schema,
        LLMAnalysisContext,
        LLMStructurePolicy,
        LLMStructureSuggestion,
        SemanticPlanProposal,
        SemanticFieldProposal,
        SemanticEntityProposal,
        SemanticSelector,
        SemanticPathStep,
        LLMPlanProvenance,
        SourceTextSpan,
        DocumentSpanSelector,
        DocumentSpanGrouping,
        QuotedSpan,
        DocumentFieldProposal,
        DocumentEntityProposal,
        DocumentEntitySuggestion,
        SemanticParseReport,
    ],
)
def test_new_public_objects_have_russian_docstrings(
    public_object: type[object],
) -> None:
    members: list[object] = [public_object]
    members.extend(
        member
        for name, member in vars(public_object).items()
        if not name.startswith("_")
        and (inspect.isfunction(member) or isinstance(member, property))
    )
    for member in members:
        doc = inspect.getdoc(member)
        assert doc is not None and re.search(r"[А-Яа-яЁё]", doc), member


def test_m06_analyzer_example_runs_with_real_validation_offline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[4]
    documentation = (root / "docs" / "llm-semantic-parsing.md").read_text()
    match = re.search(
        r"<!-- example:m06-analyzer:start -->\s*```python\n(?P<code>.*?)\n```",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None
    guard = (
        "import sys\n"
        "def no_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Network запрещена в примере M6')\n"
        "sys.addaudithook(no_network)\n"
        f"sys.path.insert(0, {str(root / 'packages' / 'structuraguard')!r})\n"
    )
    driver = """
import asyncio
from functools import partial
from tests.fakes.llm import fixed_clock
from tests.fakes.semantic import Scanner, context, encoded, scenario
from tests.unit.structure.test_execution import stream
from structuraguard.contracts import StructurePlanCreated
from structuraguard.llm import FakeLLMProvider, ScriptedResponse
from structuraguard.parsers.builtin import DelimitedTextParser

async def main():
    request, batches, output = await scenario(DelimitedTextParser(), b"name,n\\nAda,1\\nBob,2\\n")
    provider = FakeLLMProvider((ScriptedResponse(output_json=encoded(output)),), clock=fixed_clock)
    result = await analyze_structure(request, partial(stream, batches), provider, Scanner(), context())
    assert isinstance(result, StructurePlanCreated)
    assert result.plan.semantic_analysis.prompt.version == "1.0.0"
    assert provider.call_count == 1
    print(result.kind)

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", guard + match.group("code") + "\n" + driver],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == "" and result.stdout == "plan_created\n"
