from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "structuraguard"

_ALLOWED_EXTERNAL_ROOTS = {
    "contracts": frozenset(
        {
            "__future__",
            "collections",
            "datetime",
            "decimal",
            "enum",
            "hashlib",
            "json",
            "math",
            "pydantic",
            "pydantic_core",
            "re",
            "typing",
            "unicodedata",
        }
    ),
    "domain": frozenset({"__future__", "hashlib"}),
    "ports": frozenset({"__future__", "collections", "dataclasses", "typing"}),
}


def _assert_import_allowed(layer: str, module: str, source_file: Path) -> None:
    if module.startswith(f"structuraguard.{layer}"):
        return
    if layer in {"domain", "ports"} and module.startswith("structuraguard.contracts"):
        return
    root = module.partition(".")[0]
    assert root in _ALLOWED_EXTERNAL_ROOTS[layer], (
        f"{layer} imports forbidden dependency {module!r} in {source_file}"
    )


def _assert_relative_import_allowed(
    layer: str,
    imported: ast.ImportFrom,
    source_file: Path,
) -> None:
    assert imported.level == 1, (
        f"{layer} relative import escapes its layer in {source_file}"
    )
    resolved = f"structuraguard.{layer}"
    if imported.module:
        resolved = f"{resolved}.{imported.module}"
    _assert_import_allowed(layer, resolved, source_file)


def test_m2_layers_do_not_import_infrastructure_or_facades() -> None:
    for layer in ("contracts", "domain", "ports"):
        source_files = tuple(sorted((PACKAGE_ROOT / layer).glob("*.py")))
        assert source_files
        for source_file in source_files:
            tree = ast.parse(
                source_file.read_text(encoding="utf-8"), filename=str(source_file)
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for imported in node.names:
                        _assert_import_allowed(layer, imported.name, source_file)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        _assert_relative_import_allowed(layer, node, source_file)
                        continue
                    _assert_import_allowed(layer, node.module or "", source_file)


def test_relative_import_cannot_escape_contract_layer() -> None:
    imported = ast.ImportFrom(module="sdk", names=[], level=2)

    with pytest.raises(AssertionError):
        _assert_relative_import_allowed(
            "contracts",
            imported,
            PACKAGE_ROOT / "contracts" / "example.py",
        )


def test_m2_contract_layers_contain_no_dynamic_execution_primitives() -> None:
    forbidden_calls = {"eval", "exec", "compile", "__import__"}

    for layer in ("contracts", "domain", "ports"):
        for source_file in sorted((PACKAGE_ROOT / layer).glob("*.py")):
            tree = ast.parse(
                source_file.read_text(encoding="utf-8"), filename=str(source_file)
            )
            called_names = {
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            assert called_names.isdisjoint(forbidden_calls), source_file
