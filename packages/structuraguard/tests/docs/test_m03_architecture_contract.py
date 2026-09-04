from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
ARCHITECTURE_PATH = REPOSITORY_ROOT / "docs" / "architecture.md"
PUBLIC_API_PATH = REPOSITORY_ROOT / "docs" / "public-api.md"
THREAT_MODEL_PATH = REPOSITORY_ROOT / "docs" / "threat-model.md"
M03_REGISTRY_EXAMPLE = re.compile(
    r"<!-- example:m03-registry:start -->\s*"
    r"```python\n(?P<code>.*?)\n```\s*"
    r"<!-- example:m03-registry:end -->",
    flags=re.DOTALL,
)
M03_DISCOVERY_EXAMPLE = re.compile(
    r"<!-- example:m03-discovery:start -->\s*"
    r"```python\n(?P<code>.*?)\n```\s*"
    r"<!-- example:m03-discovery:end -->",
    flags=re.DOTALL,
)


def test_m03_registry_documentation_example_is_executable(tmp_path: Path) -> None:
    documentation = PUBLIC_API_PATH.read_text(encoding="utf-8")
    match = M03_REGISTRY_EXAMPLE.search(documentation)

    assert match is not None, "Копируемый M3 registry-пример отсутствует"

    completed = subprocess.run(
        [sys.executable, "-c", match.group("code")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == "demo.parser\n"


def test_m03_discovery_documentation_example_is_descriptor_only(
    tmp_path: Path,
) -> None:
    documentation = PUBLIC_API_PATH.read_text(encoding="utf-8")
    match = M03_DISCOVERY_EXAMPLE.search(documentation)

    assert match is not None, "Копируемый M3 discovery-пример отсутствует"

    dist_info = tmp_path / "sample_plugin-1.4.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: sample-plugin\nVersion: 1.4.0\n\n",
        encoding="utf-8",
    )
    (dist_info / "entry_points.txt").write_text(
        "[structuraguard.parsers]\nparser.sample = vendor.sample:SampleParser\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, "-c", match.group("code")],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert completed.stdout == (
        "parser.sample sample-plugin vendor.sample SampleParser\n"
    )


def test_m03_docs_fix_parser_registry_and_output_boundaries() -> None:
    architecture = ARCHITECTURE_PATH.read_text(encoding="utf-8")
    public_api = PUBLIC_API_PATH.read_text(encoding="utf-8")
    threat_model = THREAT_MODEL_PATH.read_text(encoding="utf-8")

    assert "module-level mutable registry" in architecture
    assert "PARSER_FORMAT_CONFLICT" in architecture
    assert "exact group `structuraguard.parsers`" in threat_model
    assert "`EntryPoint.load()`" in threat_model
    assert "`activate_plugin()`" in threat_model
    assert "typed `PARSER_FORMAT_CONFLICT`" in threat_model
    assert "`SecurityEvent` и policy routing остаются `PLANNED`" in threat_model
    assert "AsyncIterator[ExtractedBatch]" in public_api
    assert "Parser не получает LLM/DB authority" in public_api
    assert "таблицу назначения" in public_api
    assert "M3 не реализует orchestrator" in public_api
