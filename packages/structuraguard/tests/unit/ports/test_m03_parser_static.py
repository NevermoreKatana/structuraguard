from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PARSERS_ROOT = (
    REPOSITORY_ROOT
    / "packages"
    / "structuraguard"
    / "src"
    / "structuraguard"
    / "parsers"
)
PACKAGE_PROJECT = REPOSITORY_ROOT / "packages" / "structuraguard" / "pyproject.toml"


@pytest.mark.parametrize("return_type", ("MappingPlan", "NormalizedBatch"))
def test_mypy_rejects_parser_that_returns_semantic_output(
    tmp_path: Path,
    return_type: str,
) -> None:
    consumer = tmp_path / "semantic_parser.py"
    consumer.write_text(
        f"""
from collections.abc import AsyncIterator

from structuraguard.contracts import (
    MappingPlan,
    NormalizedBatch,
    ProbeResult,
    SourceArtifact,
)
from structuraguard.ports import Parser
from structuraguard.ports.source import ParseContext, ProbeContext


class SemanticParser:
    adapter_id = "semantic.parser"
    version = "1.0.0"
    priority = 0

    async def probe(
        self, source: SourceArtifact, context: ProbeContext
    ) -> ProbeResult:
        raise NotImplementedError

    def parse(
        self, source: SourceArtifact, context: ParseContext
    ) -> AsyncIterator[{return_type}]:
        raise NotImplementedError


parser: Parser = SemanticParser()
""".lstrip(),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(REPOSITORY_ROOT / "pyproject.toml"),
            "--strict",
            str(consumer),
        ],
        check=False,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Incompatible types in assignment" in completed.stdout
    assert "ExtractedBatch" in completed.stdout


def test_parser_registry_layer_has_no_llm_database_or_orchestrator_dependency() -> None:
    allowed_modules = {
        "structuraguard.contracts._base",
        "structuraguard.contracts.common",
        "structuraguard.contracts.plugins",
        "structuraguard.contracts.source",
        "structuraguard.exceptions",
        "structuraguard.ports.parser",
        "structuraguard.ports.source",
    }

    for source_path in sorted(PARSERS_ROOT.rglob("*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            imported_modules: tuple[str, ...]
            if isinstance(node, ast.ImportFrom):
                assert node.level <= 1, (source_path, node.module)
                if node.level == 1:
                    continue
                if node.module is None:
                    continue
                imported_modules = (node.module,)
            elif isinstance(node, ast.Import):
                imported_modules = tuple(alias.name for alias in node.names)
            else:
                continue

            for module in imported_modules:
                if not module.startswith("structuraguard"):
                    continue
                assert module in allowed_modules or module.startswith(
                    "structuraguard.parsers."
                ), (source_path, module)


def test_m04_parser_implementations_have_no_analysis_or_plugin_execution_path() -> None:
    forbidden_import_roots = {
        "aiohttp",
        "http",
        "httpx",
        "pip",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    }
    forbidden_calls = {
        "Popen",
        "__import__",
        "check_call",
        "exec",
        "eval",
        "import_module",
        "load",
        "run",
        "system",
        "urlopen",
    }
    source_paths = tuple(sorted(PARSERS_ROOT.rglob("*.py")))

    assert source_paths
    for source_path in source_paths:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        # Единственное opt-in исключение network boundary описано в ADR 0007.
        allowed_import_roots = {"_tika_http.py": {"httpx"}, "tika.py": {"urllib"}}.get(
            source_path.relative_to(PARSERS_ROOT).as_posix(), set()
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                method_names = {
                    child.name
                    for child in node.body
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                }
                assert "analyze" not in method_names, source_path
            elif isinstance(node, ast.Import):
                imported_roots = {alias.name.partition(".")[0] for alias in node.names}
                assert imported_roots.isdisjoint(
                    forbidden_import_roots - allowed_import_roots
                ), source_path
            elif isinstance(node, ast.ImportFrom):
                imported_root = (node.module or "").partition(".")[0]
                assert (
                    imported_root not in forbidden_import_roots - allowed_import_roots
                ), source_path
            elif isinstance(node, ast.Call):
                called_name: str | None = None
                if isinstance(node.func, ast.Name):
                    called_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    called_name = node.func.attr
                assert called_name not in forbidden_calls, source_path

    assert "structuraguard.parsers" not in PACKAGE_PROJECT.read_text(encoding="utf-8")
