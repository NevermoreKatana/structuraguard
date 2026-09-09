from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from structuraguard.parsers import builtin, tika

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
PUBLIC_API_PATH = REPOSITORY_ROOT / "docs" / "public-api.md"


@pytest.mark.parametrize(
    ("example", "expected", "verification"),
    (
        ("m04-extraction", "builtin.text\n2 batches, 2 lines\n", ""),
        (
            "m04-tika-config",
            "",
            "\nassert not TikaParserAdapter().config.enabled\n"
            "approval = TikaEgressApproval(source_fingerprint='sha256:' + 'a' * 64)\n"
            "adapter = build_tika_fallback(\n"
            "    endpoint='http://127.0.0.1:9998/tika',\n"
            "    server_version='0.0.0', approval=approval,\n"
            ")\n"
            "assert adapter.approval is approval\n"
            "assert adapter.config.expected_server_version == '0.0.0'\n"
            "assert not approval.secrets_checked\n",
        ),
    ),
)
def test_m04_documentation_example_is_executable(
    tmp_path: Path, example: str, expected: str, verification: str
) -> None:
    documentation = PUBLIC_API_PATH.read_text(encoding="utf-8")
    pattern = (
        rf"<!-- example:{example}:start -->\s*"
        r"```python\n(?P<code>.*?)\n```\s*"
        rf"<!-- example:{example}:end -->"
    )
    match = re.search(pattern, documentation, flags=re.DOTALL)
    assert match is not None, f"Копируемый пример {example} отсутствует"

    # Примеры должны выполняться офлайн, даже если позже в них добавят HTTP-код.
    network_guard = (
        "import sys\n"
        "def deny_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo'}:\n"
        "        raise AssertionError('Сеть запрещена в примерах M4')\n"
        "sys.addaudithook(deny_network)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", network_guard + match.group("code") + verification],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == expected


@pytest.mark.parametrize("name", builtin.__all__ + tika.__all__)
def test_m04_public_parser_api_has_russian_docstrings(name: str) -> None:
    module = builtin if name in builtin.__all__ else tika
    public_object = getattr(module, name)
    objects = [public_object]
    if inspect.isclass(public_object):
        objects.extend(
            method
            for method_name, method in vars(public_object).items()
            if not method_name.startswith("_")
            and (inspect.isfunction(method) or isinstance(method, property))
        )
    for public_member in objects:
        doc = inspect.getdoc(public_member)
        assert doc is not None, f"У {name} отсутствует public docstring"
        assert re.search(r"[А-Яа-яЁё]", doc), f"Docstring {name} должен быть на русском"
