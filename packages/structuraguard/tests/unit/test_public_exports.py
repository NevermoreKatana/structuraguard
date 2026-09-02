from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import structuraguard
from structuraguard import (
    AsyncStructuraGuard,
    DatabaseInspectionError,
    LoadError,
    MappingError,
    OperationNotImplementedError,
    ParserError,
    SDKConfig,
    SecurityPolicyError,
    SourceError,
    StructuraGuard,
    StructuraGuardError,
    ValidationError,
)
from structuraguard.config import SDKConfig as ConfigFromModule
from structuraguard.exceptions import (
    DatabaseInspectionError as DatabaseInspectionErrorFromModule,
)
from structuraguard.exceptions import LoadError as LoadErrorFromModule
from structuraguard.exceptions import MappingError as MappingErrorFromModule
from structuraguard.exceptions import (
    OperationNotImplementedError as OperationNotImplementedErrorFromModule,
)
from structuraguard.exceptions import ParserError as ParserErrorFromModule
from structuraguard.exceptions import (
    SecurityPolicyError as SecurityPolicyErrorFromModule,
)
from structuraguard.exceptions import SourceError as SourceErrorFromModule
from structuraguard.exceptions import (
    StructuraGuardError as StructuraGuardErrorFromModule,
)
from structuraguard.exceptions import ValidationError as ValidationErrorFromModule
from structuraguard.sdk import AsyncStructuraGuard as AsyncFacadeFromModule
from structuraguard.sync_sdk import StructuraGuard as SyncFacadeFromModule

EXPECTED_PUBLIC_EXPORTS = frozenset(
    {
        "AsyncStructuraGuard",
        "DatabaseInspectionError",
        "LoadError",
        "MappingError",
        "OperationNotImplementedError",
        "ParserError",
        "SDKConfig",
        "SecurityPolicyError",
        "SourceError",
        "StructuraGuard",
        "StructuraGuardError",
        "ValidationError",
    }
)


def test_top_level_exports_are_explicit_and_complete() -> None:
    exports = tuple(structuraguard.__all__)

    assert len(exports) == len(set(exports))
    assert frozenset(exports) == EXPECTED_PUBLIC_EXPORTS


def test_top_level_exports_reference_the_canonical_definitions() -> None:
    assert SDKConfig is ConfigFromModule
    assert AsyncStructuraGuard is AsyncFacadeFromModule
    assert StructuraGuard is SyncFacadeFromModule
    assert StructuraGuardError is StructuraGuardErrorFromModule
    assert OperationNotImplementedError is OperationNotImplementedErrorFromModule
    assert SourceError is SourceErrorFromModule
    assert ParserError is ParserErrorFromModule
    assert DatabaseInspectionError is DatabaseInspectionErrorFromModule
    assert MappingError is MappingErrorFromModule
    assert ValidationError is ValidationErrorFromModule
    assert SecurityPolicyError is SecurityPolicyErrorFromModule
    assert LoadError is LoadErrorFromModule


@pytest.mark.parametrize("helper_name", ("TYPE_CHECKING", "cast", "import_module"))
def test_runtime_helpers_are_not_exposed(helper_name: str) -> None:
    assert helper_name not in vars(structuraguard)
    assert helper_name not in dir(structuraguard)

    completed = subprocess.run(
        [sys.executable, "-c", f"from structuraguard import {helper_name}"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "ImportError" in completed.stderr


def test_mypy_rejects_unknown_top_level_export(tmp_path: Path) -> None:
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from structuraguard import SDKConfg\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(consumer)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "SDKConfg" in completed.stdout
    assert "[attr-defined]" in completed.stdout
