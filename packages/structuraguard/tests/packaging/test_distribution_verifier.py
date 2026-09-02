from __future__ import annotations

import io
import runpy
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import FunctionType, SimpleNamespace
from typing import Protocol, cast

import pytest

VERIFIER_PATH = Path(__file__).resolve().parents[4] / "scripts/verify_distribution.py"
REPOSITORY_ROOT = VERIFIER_PATH.parents[1]
VERIFIER_NAMESPACE = runpy.run_path(str(VERIFIER_PATH))
VerificationError = cast(type[Exception], VERIFIER_NAMESPACE["VerificationError"])
read_sdist_function = cast(
    FunctionType,
    VERIFIER_NAMESPACE["_read_sdist"],
)
read_sdist = cast(Callable[[Path], dict[str, bytes]], read_sdist_function)
select_artifacts = cast(
    Callable[[Path], tuple[Path, Path]],
    VERIFIER_NAMESPACE["_select_artifacts"],
)
locked_registry_version = cast(
    Callable[[Path, str], str],
    VERIFIER_NAMESPACE["_locked_registry_version"],
)
runtime_closure = cast(
    frozenset[str],
    VERIFIER_NAMESPACE["_PYDANTIC_RUNTIME_CLOSURE"],
)
verify_installed_wheel_function = cast(
    FunctionType,
    VERIFIER_NAMESPACE["_verify_installed_wheel"],
)


class VerifyInstalledWheel(Protocol):
    def __call__(
        self,
        *,
        uv: Path,
        wheel: Path,
        expected_runtime_versions: dict[str, str],
        temp_root: Path,
        repository_root: Path,
    ) -> None: ...


verify_installed_wheel = cast(
    VerifyInstalledWheel,
    verify_installed_wheel_function,
)


def test_sdist_member_limit_is_applied_during_streaming_read(tmp_path: Path) -> None:
    archive_path = tmp_path / "too-many-members.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        for index in range(513):
            member = tarfile.TarInfo(f"structuraguard-0.1.0/file-{index}.txt")
            member.size = 0
            archive.addfile(member, io.BytesIO())

    with pytest.raises(VerificationError, match="слишком много archive members"):
        read_sdist(archive_path)


def test_sdist_rejects_pax_records_before_tarfile_processes_them(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "pax-record-bomb.tar.gz"
    pax_records = b"5 x=\n" * 100_000
    with tarfile.open(archive_path, mode="w:gz") as archive:
        member = tarfile.TarInfo("././@PaxHeader")
        member.type = tarfile.XHDTYPE
        member.size = len(pax_records)
        archive.addfile(member, io.BytesIO(pax_records))

    with pytest.raises(VerificationError, match="extended и special tar headers"):
        read_sdist(archive_path)


def test_sdist_decompressed_stream_limit_covers_regular_member_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_path = tmp_path / "stream-limit.tar.gz"
    payload = b"x" * 8_192
    with tarfile.open(archive_path, mode="w:gz") as archive:
        member = tarfile.TarInfo("structuraguard-0.1.0/file.txt")
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))

    monkeypatch.setitem(
        read_sdist_function.__globals__,
        "_MAX_TAR_STREAM_SIZE",
        4_096,
    )
    with pytest.raises(VerificationError, match="Decompressed tar stream"):
        read_sdist(archive_path)


def test_dist_directory_rejects_gitignore(tmp_path: Path) -> None:
    (tmp_path / "structuraguard-0.1.0-py3-none-any.whl").touch()
    (tmp_path / "structuraguard-0.1.0.tar.gz").touch()
    (tmp_path / ".gitignore").touch()

    with pytest.raises(VerificationError, match="неожиданные файлы"):
        select_artifacts(tmp_path)


def test_wheel_smoke_does_not_resolve_runtime_by_name_offline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_versions = {
        package_name: locked_registry_version(REPOSITORY_ROOT, package_name)
        for package_name in sorted(runtime_closure)
    }
    exact_requirements = tuple(
        f"{package_name}=={version}"
        for package_name, version in runtime_versions.items()
    )
    fake_python = tmp_path / "python"
    fake_python.touch()

    class FakeEnvBuilder:
        def __init__(self, **_: object) -> None:
            pass

        def create(self, _: Path) -> None:
            pass

    def fake_venv_python(_: Path) -> Path:
        return fake_python

    calls: list[tuple[list[str], Path, Path | None]] = []

    def fake_run_checked(
        command: list[str],
        *,
        cwd: Path,
        project_environment: Path | None = None,
    ) -> None:
        calls.append((command, cwd, project_environment))
        resolves_from_registry = command[1:3] == ["pip", "install"] and any(
            requirement in command for requirement in exact_requirements
        )
        if resolves_from_registry:
            raise VerificationError("simulated empty registry index cache")

    verifier_globals = verify_installed_wheel_function.__globals__
    monkeypatch.setitem(
        verifier_globals,
        "venv",
        SimpleNamespace(EnvBuilder=FakeEnvBuilder),
    )
    monkeypatch.setitem(verifier_globals, "_venv_python", fake_venv_python)
    monkeypatch.setitem(verifier_globals, "_run_checked", fake_run_checked)

    wheel = tmp_path / "structuraguard-0.1.0-py3-none-any.whl"
    wheel.touch()
    verify_installed_wheel(
        uv=Path("/tool/uv"),
        wheel=wheel,
        expected_runtime_versions=runtime_versions,
        temp_root=tmp_path,
        repository_root=REPOSITORY_ROOT,
    )

    sync_calls = [call for call in calls if call[0][1:2] == ["sync"]]
    assert sync_calls == [
        (
            [
                "/tool/uv",
                "sync",
                "--offline",
                "--no-config",
                "--no-python-downloads",
                "--locked",
                "--project",
                str(REPOSITORY_ROOT),
                "--package",
                "structuraguard",
                "--no-default-groups",
                "--no-install-workspace",
                "--python",
                str(fake_python),
            ],
            tmp_path,
            tmp_path / "venv",
        )
    ]
    wheel_install_calls = [
        call
        for call in calls
        if call[0][1:3] == ["pip", "install"] and str(wheel) in call[0]
    ]
    assert wheel_install_calls == [
        (
            [
                "/tool/uv",
                "pip",
                "install",
                "--offline",
                "--no-config",
                "--no-python-downloads",
                "--python",
                str(fake_python),
                "--no-deps",
                str(wheel),
            ],
            tmp_path,
            None,
        )
    ]
