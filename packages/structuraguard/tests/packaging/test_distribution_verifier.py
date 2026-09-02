from __future__ import annotations

import io
import runpy
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import FunctionType
from typing import cast

import pytest

VERIFIER_PATH = Path(__file__).resolve().parents[4] / "scripts/verify_distribution.py"
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
