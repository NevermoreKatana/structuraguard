from __future__ import annotations

import inspect
import subprocess
import sys
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import get_origin, get_type_hints

import pytest

import structuraguard.ports as ports
from structuraguard.ports.source import ParseContext, ProbeContext, SourceReader

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]

_PORT_EXPORTS = frozenset(
    {
        "CandidateMapper",
        "NormalizedDataProfiler",
        "PIIClassifier",
        "AuditStore",
        "DatabaseAdapter",
        "LLMProvider",
        "ParsePlanExecutor",
        "ParsePlanValidator",
        "Parser",
        "SecurityScanner",
        "SemanticStructureAnalyzer",
        "StagingStore",
        "StructuralProfiler",
    }
)


class _FakeParser:
    adapter_id = "fake.parser"
    version = "1.0.0"
    priority = 10

    async def probe(self, source: object, context: object) -> object:
        raise NotImplementedError

    def parse(self, source: object, context: object) -> AsyncIterator[object]:
        async def batches() -> AsyncIterator[object]:
            items: tuple[object, ...] = ()
            for item in items:
                yield item

        return batches()


class _FakeSemanticStructureAnalyzer:
    async def analyze(self, request: object) -> object:
        raise NotImplementedError


class _FakeParsePlanValidator:
    def validate(self, request: object) -> object:
        raise NotImplementedError


class _FakeParsePlanExecutor:
    def execute(
        self,
        batches: AsyncIterable[object],
        plan: object,
        context: object,
    ) -> AsyncIterator[object]:
        async def normalized_batches() -> AsyncIterator[object]:
            async for item in batches:
                yield item

        return normalized_batches()


class _FakeDatabaseAdapter:
    async def inspect(self, request: object) -> object:
        raise NotImplementedError

    async def execute(
        self,
        batches: AsyncIterable[object],
        plan: object,
        context: object,
    ) -> object:
        raise NotImplementedError


class _FakeLLMProvider:
    capabilities = ("structured_output",)

    async def generate_structured(self, request: object) -> object:
        raise NotImplementedError


class _FakeSecurityScanner:
    async def scan(self, request: object) -> object:
        raise NotImplementedError


class _FakeStagingStore:
    async def stage(self, batch: object, context: object) -> None:
        raise NotImplementedError


class _FakeAuditStore:
    async def append(self, event: object) -> None:
        raise NotImplementedError


class _IncompleteAdapter:
    pass


class _FakeSourceReader:
    def __init__(self, source_fingerprint: str) -> None:
        self._source_fingerprint = source_fingerprint

    @property
    def source_fingerprint(self) -> str:
        return self._source_fingerprint

    async def read(self, *, offset: int, size: int) -> bytes:
        return b""


def test_protocol_exports_are_explicit_and_minimal() -> None:
    exports = tuple(ports.__all__)

    assert frozenset(exports) == _PORT_EXPORTS
    assert len(exports) == len(set(exports))


def test_parser_identity_contract_is_read_only() -> None:
    for attribute_name in ("adapter_id", "version", "priority"):
        assert isinstance(
            inspect.getattr_static(ports.Parser, attribute_name),
            property,
        )


def test_source_reader_and_runtime_limits_are_fingerprint_bound_and_immutable() -> None:
    fingerprint = "sha256:" + "a" * 64
    reader = _FakeSourceReader(fingerprint)

    assert isinstance(reader, SourceReader)
    assert isinstance(
        inspect.getattr_static(SourceReader, "source_fingerprint"), property
    )
    probe = ProbeContext(
        reader=reader,
        source_fingerprint=fingerprint,
        max_probe_bytes=1024,
    )
    ParseContext(
        reader=reader,
        source_fingerprint=fingerprint,
        max_bytes=4096,
        max_records=100,
        max_nesting_depth=16,
    )

    with pytest.raises(FrozenInstanceError):
        probe.max_probe_bytes = 2048  # type: ignore[misc]
    with pytest.raises(ValueError):
        ProbeContext(
            reader=reader,
            source_fingerprint="not-a-fingerprint",
            max_probe_bytes=1024,
        )
    with pytest.raises(ValueError):
        ParseContext(
            reader=reader,
            source_fingerprint=fingerprint,
            max_bytes=True,
            max_records=100,
            max_nesting_depth=16,
        )


def test_runtime_protocols_accept_structurally_compatible_adapters() -> None:
    assert isinstance(_FakeParser(), ports.Parser)
    assert isinstance(_FakeSemanticStructureAnalyzer(), ports.SemanticStructureAnalyzer)
    assert isinstance(_FakeParsePlanValidator(), ports.ParsePlanValidator)
    assert isinstance(_FakeParsePlanExecutor(), ports.ParsePlanExecutor)
    assert isinstance(_FakeDatabaseAdapter(), ports.DatabaseAdapter)
    assert isinstance(_FakeLLMProvider(), ports.LLMProvider)
    assert isinstance(_FakeSecurityScanner(), ports.SecurityScanner)
    assert isinstance(_FakeStagingStore(), ports.StagingStore)
    assert isinstance(_FakeAuditStore(), ports.AuditStore)


def test_runtime_protocols_reject_missing_capabilities() -> None:
    incomplete = _IncompleteAdapter()

    assert not isinstance(incomplete, ports.Parser)
    assert not isinstance(incomplete, ports.SemanticStructureAnalyzer)
    assert not isinstance(incomplete, ports.ParsePlanValidator)
    assert not isinstance(incomplete, ports.ParsePlanExecutor)
    assert not isinstance(incomplete, ports.DatabaseAdapter)
    assert not isinstance(incomplete, ports.LLMProvider)
    assert not isinstance(incomplete, ports.SecurityScanner)
    assert not isinstance(incomplete, ports.StagingStore)
    assert not isinstance(incomplete, ports.AuditStore)


@pytest.mark.parametrize(
    ("protocol", "method_name", "is_coroutine", "is_streaming"),
    (
        (ports.Parser, "probe", True, False),
        (ports.Parser, "parse", False, True),
        (ports.SemanticStructureAnalyzer, "analyze", True, False),
        (ports.ParsePlanValidator, "validate", False, False),
        (ports.ParsePlanExecutor, "execute", False, True),
        (ports.DatabaseAdapter, "inspect", True, False),
        (ports.DatabaseAdapter, "execute", True, False),
        (ports.LLMProvider, "generate_structured", True, False),
        (ports.SecurityScanner, "scan", True, False),
        (ports.StagingStore, "stage", True, False),
        (ports.AuditStore, "append", True, False),
    ),
)
def test_protocol_coroutine_and_streaming_call_shapes_are_stable(
    protocol: type[object],
    method_name: str,
    is_coroutine: bool,
    is_streaming: bool,
) -> None:
    method = getattr(protocol, method_name)
    return_annotation = get_type_hints(method)["return"]

    assert inspect.iscoroutinefunction(method) is is_coroutine
    assert (get_origin(return_annotation) is AsyncIterator) is is_streaming


def test_mypy_rejects_executor_that_accepts_unvalidated_parse_plan(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "invalid_executor.py"
    consumer.write_text(
        """
from collections.abc import AsyncIterable, AsyncIterator

from structuraguard.contracts import (
    ExtractedBatch,
    NormalizedBatch,
    ParseExecutionContext,
    ParsePlan,
)
from structuraguard.ports import ParsePlanExecutor


class UnsafeExecutor:
    def execute(
        self,
        batches: AsyncIterable[ExtractedBatch],
        plan: ParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncIterator[NormalizedBatch]:
        raise NotImplementedError


executor: ParsePlanExecutor = UnsafeExecutor()
""".lstrip(),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(REPOSITORY_ROOT / "pyproject.toml"),
            "--strict",
            str(consumer),
        ],
        check=False,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Incompatible types in assignment" in completed.stdout
    assert "ValidatedParsePlan" in completed.stdout


def test_mypy_rejects_database_adapter_that_accepts_raw_mapping_plan(
    tmp_path: Path,
) -> None:
    consumer = tmp_path / "invalid_database_adapter.py"
    consumer.write_text(
        """
from collections.abc import AsyncIterable

from structuraguard.contracts import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    LoadContext,
    LoadReport,
    MappingPlan,
    NormalizedBatch,
)
from structuraguard.ports import DatabaseAdapter


class UnsafeDatabaseAdapter:
    async def inspect(self, request: DatabaseInspectionRequest) -> DatabaseCatalog:
        raise NotImplementedError

    async def execute(
        self,
        batches: AsyncIterable[NormalizedBatch],
        plan: MappingPlan,
        context: LoadContext,
    ) -> LoadReport:
        raise NotImplementedError


adapter: DatabaseAdapter = UnsafeDatabaseAdapter()
""".lstrip(),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(REPOSITORY_ROOT / "pyproject.toml"),
            "--strict",
            str(consumer),
        ],
        check=False,
        capture_output=True,
        cwd=REPOSITORY_ROOT,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "Incompatible types in assignment" in completed.stdout
    assert "ValidatedMappingPlan" in completed.stdout
