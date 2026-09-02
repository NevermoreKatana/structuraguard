from __future__ import annotations

import ast
import re
import sys
from importlib import metadata
from pathlib import Path

import structuraguard

FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "celery",
        "django",
        "fastapi",
        "flask",
        "pydantic_settings",
        "redis",
    }
)


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if match is None:
        raise AssertionError(f"invalid installed requirement: {requirement!r}")
    return match.group(0).lower().replace("_", "-")


def _unconditional_requirements() -> frozenset[str]:
    requirements = metadata.requires("structuraguard") or []
    return frozenset(
        _requirement_name(requirement)
        for requirement in requirements
        if "extra ==" not in requirement
    )


def _source_files() -> tuple[Path, ...]:
    package_file = structuraguard.__file__
    if package_file is None:
        raise AssertionError("structuraguard must be a filesystem package")
    return tuple(sorted(Path(package_file).resolve().parent.rglob("*.py")))


def _assert_safe_imports_and_environment_access(source_file: Path) -> None:
    tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
    environment_module_aliases: set[str] = set()
    environment_value_aliases: set[str] = set()
    environment_modules = {"nt", "os", "posix"}
    environment_attributes = {
        "environ",
        "environb",
        "getenv",
        "getenvb",
        "putenv",
        "unsetenv",
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.partition(".")[0]
                assert root not in FORBIDDEN_IMPORT_ROOTS, (
                    f"forbidden dependency import in {source_file}: {alias.name}"
                )
                if alias.name in environment_modules:
                    environment_module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.partition(".")[0]
            assert root not in FORBIDDEN_IMPORT_ROOTS, (
                f"forbidden dependency import in {source_file}: {module}"
            )
            assert not any(alias.name == "BaseSettings" for alias in node.names), (
                f"BaseSettings is forbidden in importable scaffold: {source_file}"
            )
            if module in environment_modules:
                for alias in node.names:
                    if alias.name in environment_attributes:
                        environment_value_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in environment_value_aliases, (
                f"environment access is forbidden in {source_file}: {node.id}"
            )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in environment_module_aliases
        ):
            assert node.attr not in environment_attributes, (
                f"environment access is forbidden in {source_file}: {node.value.id}.{node.attr}"
            )


def test_core_has_only_the_allowed_unconditional_dependency() -> None:
    assert _unconditional_requirements() == frozenset({"pydantic"})


def test_import_does_not_load_web_frameworks_or_settings_package() -> None:
    loaded_roots = {module_name.partition(".")[0] for module_name in sys.modules}

    assert loaded_roots.isdisjoint(FORBIDDEN_IMPORT_ROOTS)


def test_importable_source_has_no_framework_or_environment_coupling() -> None:
    source_files = _source_files()

    assert source_files
    for source_file in source_files:
        _assert_safe_imports_and_environment_access(source_file)
