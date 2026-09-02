from __future__ import annotations

import runpy
import shutil
import subprocess
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ROOT_PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
PACKAGE_PYPROJECT = REPOSITORY_ROOT / "packages" / "structuraguard" / "pyproject.toml"
BUILD_GUARD = REPOSITORY_ROOT / "scripts" / "workspace_build_guard.py"
EXPECTED_BUILD_ERROR = "Корень workspace не является Python distribution"
BUILD_GUARD_NAMESPACE = runpy.run_path(str(BUILD_GUARD))
WorkspaceBuildError = cast(
    type[Exception],
    BUILD_GUARD_NAMESPACE["WorkspaceBuildError"],
)


def _load_pyproject(path: Path) -> Mapping[str, object]:
    return cast(
        Mapping[str, object],
        tomllib.loads(path.read_text(encoding="utf-8")),
    )


def _as_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise AssertionError(f"{label} must be a TOML table")
    return cast(Mapping[str, object], value)


def _snapshot_tree(root: Path) -> dict[str, bytes | None]:
    return {
        path.relative_to(root).as_posix(): None if path.is_dir() else path.read_bytes()
        for path in sorted(root.rglob("*"))
        if "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def _copy_workspace_for_build(checkout: Path) -> None:
    checkout.mkdir()
    shutil.copy2(ROOT_PYPROJECT, checkout / "pyproject.toml")
    shutil.copy2(REPOSITORY_ROOT / "uv.lock", checkout / "uv.lock")
    shutil.copytree(
        REPOSITORY_ROOT / "packages",
        checkout / "packages",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if BUILD_GUARD.is_file():
        scripts = checkout / "scripts"
        scripts.mkdir()
        shutil.copy2(BUILD_GUARD, scripts / BUILD_GUARD.name)


def test_workspace_root_is_virtual_and_has_one_buildable_member() -> None:
    root = _load_pyproject(ROOT_PYPROJECT)
    uv = _as_mapping(root.get("tool"), "tool")
    uv = _as_mapping(uv.get("uv"), "tool.uv")
    workspace = _as_mapping(uv.get("workspace"), "tool.uv.workspace")
    package = _load_pyproject(PACKAGE_PYPROJECT)
    build_system = _as_mapping(root.get("build-system"), "build-system")

    assert "project" not in root
    assert build_system == {
        "requires": [],
        "build-backend": "workspace_build_guard",
        "backend-path": ["scripts"],
    }
    assert uv["package"] is False
    assert workspace["members"] == ["packages/structuraguard"]
    assert _as_mapping(package.get("project"), "package project")["name"] == (
        "structuraguard"
    )
    assert "build-system" in package


@pytest.mark.parametrize(
    ("hook_name", "arguments"),
    [
        ("get_requires_for_build_sdist", ({"mode": "root"},)),
        ("build_sdist", ("dist", {"mode": "root"})),
        ("get_requires_for_build_wheel", ({"mode": "root"},)),
        ("prepare_metadata_for_build_wheel", ("metadata", {"mode": "root"})),
        ("build_wheel", ("dist", {"mode": "root"}, "metadata")),
        ("get_requires_for_build_editable", ({"mode": "root"},)),
        ("prepare_metadata_for_build_editable", ("metadata", {"mode": "root"})),
        ("build_editable", ("dist", {"mode": "root"}, "metadata")),
    ],
)
def test_workspace_build_guard_rejects_every_build_hook(
    hook_name: str,
    arguments: tuple[object, ...],
) -> None:
    hook = cast(Callable[..., object], BUILD_GUARD_NAMESPACE[hook_name])

    with pytest.raises(WorkspaceBuildError, match=EXPECTED_BUILD_ERROR):
        hook(*arguments)


def test_generic_root_build_is_rejected_without_artifacts_or_debris(
    tmp_path: Path,
) -> None:
    uv_executable = shutil.which("uv")
    assert uv_executable is not None, "uv must be available for the packaging contract"
    checkout = tmp_path / "checkout"
    artifacts = tmp_path / "artifacts"
    _copy_workspace_for_build(checkout)
    before = _snapshot_tree(checkout)

    result = subprocess.run(
        [
            uv_executable,
            "build",
            "--offline",
            "--no-python-downloads",
            "--no-create-gitignore",
            "--out-dir",
            str(artifacts),
            str(checkout),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert EXPECTED_BUILD_ERROR in result.stdout + result.stderr
    assert not artifacts.exists() or not any(artifacts.iterdir())
    assert _snapshot_tree(checkout) == before
    assert not any(
        path.name in {"build", "dist"}
        or path.name.endswith((".egg-info", ".whl", ".tar.gz"))
        for path in checkout.rglob("*")
    )
