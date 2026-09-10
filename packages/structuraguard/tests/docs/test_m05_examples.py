"""Копируемый пример M5 исполняется офлайн из текста документации."""

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from structuraguard import structure
from structuraguard.contracts.analysis import AnalysisScore
from structuraguard.contracts.execution import ExecutionStage, SelectionOperation

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def test_m05_documentation_example_is_executable_offline(tmp_path: Path) -> None:
    documentation = (REPOSITORY_ROOT / "docs" / "structure.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"<!-- example:m05-normalization:start -->\s*"
        r"```python\n(?P<code>.*?)\n```\s*"
        r"<!-- example:m05-normalization:end -->",
        documentation,
        flags=re.DOTALL,
    )
    assert match is not None, "Копируемый пример M5 отсутствует"
    network_guard = (
        "import sys\n"
        "def deny_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Сеть запрещена в примере M5')\n"
        "sys.addaudithook(deny_network)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", network_guard + match.group("code")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == (
        "2 records; provenance preserved; semantics unresolved\n"
    )


@pytest.mark.parametrize(
    "public_object",
    (
        *(getattr(structure, name) for name in structure.__all__),
        AnalysisScore,
        ExecutionStage,
        SelectionOperation,
    ),
)
def test_m05_public_api_has_russian_docstrings(public_object: object) -> None:
    members = [public_object]
    if inspect.isclass(public_object):
        members.extend(
            member
            for name, member in vars(public_object).items()
            if not name.startswith("_")
            and (inspect.isfunction(member) or isinstance(member, property))
        )
    for member in members:
        doc = inspect.getdoc(member)
        assert doc is not None, f"У {member} отсутствует public docstring"
        assert re.search(r"[А-Яа-яЁё]", doc), (
            f"Docstring {member} должен быть на русском"
        )
