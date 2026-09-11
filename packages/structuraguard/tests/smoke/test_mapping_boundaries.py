"""Mapper не импортирует adapters/providers и не исполняет SQL/код из metadata."""

import ast
from pathlib import Path


def test_mapper_imports_only_contracts_domain_and_explicit_pure_dependencies() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "structuraguard" / "mapping"
    pure = {
        "__future__",
        "asyncio",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "heapq",
        "pydantic",
        "re",
        "typing",
        "unicodedata",
    }
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(a.name.split(".")[0] in pure for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                module = node.module or ""
                assert (
                    module.split(".")[0] in pure
                    or module.startswith(
                        ("structuraguard.contracts.", "structuraguard.domain.")
                    )
                    or module == "structuraguard.exceptions"
                )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {
                    "open",
                    "eval",
                    "exec",
                    "compile",
                    "__import__",
                }
