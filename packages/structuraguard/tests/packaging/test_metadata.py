from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from importlib import metadata
from pathlib import Path
from typing import cast

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
SOURCE_PACKAGE = PACKAGE_ROOT / "src" / "structuraguard"

EXPECTED_EXTRAS = frozenset(
    {
        "postgres",
        "pdf",
        "excel",
        "office",
        "litellm",
        "llm",
        "tika",
        "xml",
        "yaml",
        "all",
    }
)
EXPECTED_EXTRA_PACKAGES: Mapping[str, frozenset[str]] = {
    "xml": frozenset({"defusedxml"}),
    "yaml": frozenset({"pyyaml"}),
    "postgres": frozenset({"sqlalchemy", "asyncpg", "psycopg"}),
    "pdf": frozenset({"pymupdf"}),
    "excel": frozenset({"openpyxl", "defusedxml"}),
    "office": frozenset({"python-docx", "defusedxml"}),
    "litellm": frozenset({"litellm"}),
    "llm": frozenset({"httpx", "httpcore"}),
    "tika": frozenset({"httpx", "httpcore", "defusedxml"}),
}


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be a TOML table")
    return cast(Mapping[str, object], value)


def _as_string_sequence(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise AssertionError(f"{label} must be a list of strings")
    return tuple(cast(Sequence[str], value))


def _requirement_name(requirement: str) -> str:
    match = re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
    if match is None:
        raise AssertionError(f"invalid requirement: {requirement!r}")
    return match.group(0).lower().replace("_", "-")


def _load_package_project() -> Mapping[str, object]:
    document = cast(
        Mapping[str, object],
        tomllib.loads(PACKAGE_PYPROJECT.read_text(encoding="utf-8")),
    )
    return _as_mapping(document.get("project"), "project")


def test_package_metadata_declares_one_typed_python_312_distribution() -> None:
    document = cast(
        Mapping[str, object],
        tomllib.loads(PACKAGE_PYPROJECT.read_text(encoding="utf-8")),
    )
    build_system = _as_mapping(document.get("build-system"), "build-system")
    project = _as_mapping(document.get("project"), "project")

    assert project["name"] == "structuraguard"
    assert project["version"] == "0.3.0"
    assert project["requires-python"] == ">=3.12"
    assert build_system["build-backend"] == "hatchling.build"
    assert SOURCE_PACKAGE.is_dir()
    assert (SOURCE_PACKAGE / "py.typed").is_file()


def test_base_dependencies_are_exactly_pydantic_and_charset_normalizer() -> None:
    project = _load_package_project()
    dependencies = _as_string_sequence(project.get("dependencies"), "dependencies")

    assert tuple(map(_requirement_name, dependencies)) == (
        "pydantic",
        "charset-normalizer",
    )
    requirements = {_requirement_name(item): item for item in dependencies}
    assert re.search(r">=\s*2(?:\D|$)", requirements["pydantic"])
    assert re.search(r"<\s*3(?:\D|$)", requirements["pydantic"])
    assert re.search(r">=\s*3\.4(?:\D|$)", requirements["charset-normalizer"])
    assert re.search(r"<\s*4(?:\D|$)", requirements["charset-normalizer"])


def test_optional_extras_are_exact_and_all_is_the_deduplicated_union() -> None:
    project = _load_package_project()
    extras = _as_mapping(project.get("optional-dependencies"), "optional-dependencies")

    assert frozenset(extras) == EXPECTED_EXTRAS
    for extra_name, expected_packages in EXPECTED_EXTRA_PACKAGES.items():
        requirements = _as_string_sequence(extras[extra_name], extra_name)
        assert frozenset(map(_requirement_name, requirements)) == expected_packages

    all_requirements = _as_string_sequence(extras["all"], "all")
    feature_requirements = {
        requirement
        for extra_name in EXPECTED_EXTRA_PACKAGES
        for requirement in _as_string_sequence(extras[extra_name], extra_name)
    }
    assert len(all_requirements) == len(set(all_requirements))
    assert set(all_requirements) == feature_requirements


def test_tika_trace_redaction_backend_version_is_pinned() -> None:
    project = _load_package_project()
    extras = _as_mapping(project.get("optional-dependencies"), "optional-dependencies")
    for extra in ("tika", "llm", "all"):
        assert "httpcore==1.0.9" in _as_string_sequence(extras[extra], extra)


def test_installed_distribution_metadata_matches_the_project() -> None:
    distribution = metadata.distribution("structuraguard")

    assert distribution.version == "0.3.0"
    assert distribution.metadata["Requires-Python"] == ">=3.12"
