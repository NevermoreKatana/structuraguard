from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROBE_PATH = Path(__file__).with_name("import_probe.py")
SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"


@pytest.mark.parametrize("mode", ["black-box", "attribution"])
def test_import_has_no_io_or_process_side_effects(
    mode: str,
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [sys.executable, "-B", "-I", str(PROBE_PATH), mode, str(SOURCE_ROOT)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, (
        f"{mode} probe failed\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


@pytest.mark.parametrize("mode", ["black-box", "attribution"])
def test_import_probe_does_not_depend_on_pathlib_warming_urllib(
    mode: str,
    tmp_path: Path,
) -> None:
    # Python 3.12 прогревает urllib через pathlib, 3.13+ делает это отложенно.
    # Удаление только в отдельном процессе воспроизводит холодный import на всех CI.
    runner = tmp_path / "cold_import_probe.py"
    runner.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(PROBE_PATH.parent)!r})\n"
        "from import_probe import main\n"
        "for name in tuple(sys.modules):\n"
        "    if name == 'urllib' or name.startswith('urllib.'):\n"
        "        del sys.modules[name]\n"
        f"raise SystemExit(main([{mode!r}, {str(SOURCE_ROOT)!r}]))\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-I", str(runner)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"import probe passed: {mode}" in completed.stdout


def test_attribution_probe_forbids_sdk_reading_preloaded_stdlib(
    tmp_path: Path,
) -> None:
    package = tmp_path / "src" / "structuraguard"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import urllib.parse\n"
        "with open(urllib.parse.__file__, 'rb') as stream:\n"
        "    stream.read()\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-I",
            str(PROBE_PATH),
            "attribution",
            str(package.parent),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "forbidden import side effect: file access:" in completed.stderr
    assert "urllib/parse.py'" in completed.stderr.replace("\\", "/")
