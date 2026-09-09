"""Приёмочные regressions: strict OOXML и deadline Tika без arbitrary sleep."""

from __future__ import annotations

import asyncio
import sys
import traceback
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

import pytest
from tests.fakes.parsers import FakeSourceReader
from tests.fakes.tika import RTF, adapter_for
from tests.unit.parsers.builtin._document_fixtures import package_parts, zip_bytes
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers.builtin import (
    DocxParser,
    DocxParserLimits,
    XlsxParser,
    XlsxParserLimits,
    _documents,
)


@pytest.mark.anyio
@pytest.mark.parametrize("format_id", ["xlsx", "docx"])
async def test_strict_ooxml_refuses_before_source_read(
    format_id: Literal["xlsx", "docx"],
) -> None:
    content = zip_bytes(package_parts(format_id))
    source = source_for(content, display_name=f"a.{format_id}")
    reader = FakeSourceReader(source.source_fingerprint, content=content)
    parser = (
        XlsxParser(limits=XlsxParserLimits(strict_mode=True))
        if format_id == "xlsx"
        else DocxParser(limits=DocxParserLimits(strict_mode=True))
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        await collect(parser, source, contexts_for(source, content, reader=reader)[1])
    assert not reader.reads


@pytest.mark.anyio
async def test_tika_expired_deadline_does_not_cancel_consumer_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = source_for(RTF, display_name="a.rtf")
    parser, server = adapter_for(source, monkeypatch)
    stream = parser.parse(source, contexts_for(source, RTF, batch_size=1)[1])
    assert isinstance(stream, AsyncGenerator)
    try:
        first = await anext(stream)
        assert not first.is_last
        assert server.closed and server.response_closed
        loop = asyncio.get_running_loop()
        expired = loop.time() + parser.limits.timeout_seconds + 1
        with monkeypatch.context() as controlled_clock:
            controlled_clock.setattr(loop, "time", lambda: expired)
            turn = asyncio.Event()
            loop.call_soon(turn.set)
            await turn.wait()
            with pytest.raises(ParserError, match="PROCESSING_TIMEOUT"):
                await anext(stream)
    finally:
        await stream.aclose()


@pytest.mark.parametrize(
    "resource", ["secret-canary", "password=secret-canary", "cells"]
)
def test_worker_error_transport_preserves_only_allowlisted_numeric_details(
    resource: str,
) -> None:
    error = SecurityPolicyError(
        error_code="SECURITY_LIMIT_EXCEEDED",
        message="secret-canary",
        details={"resource": resource, "limit": 2, "metadata": "secret-canary"},
    )
    unit = _documents.worker_failure(error)
    restored = _documents._worker_error(unit)
    assert "secret-canary" not in unit.model_dump_json() + str(restored) + repr(
        restored.details
    )
    assert dict(restored.details) == (
        {"resource": "cells", "limit": 2} if resource == "cells" else {}
    )
    forged = _documents.DocumentUnit(
        error_code="SECURITY_LIMIT_EXCEEDED",
        error_resource="secret-canary",
        error_limit=2,
    )
    assert not _documents._worker_error(forged).details


@pytest.mark.anyio
@pytest.mark.integration
@pytest.mark.parametrize("mode", ["crash", "invalid-frame", "backend-defect"])
async def test_worker_failures_are_not_misreported_as_malformed_source(
    mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[asyncio.subprocess.Process] = []
    snapshots: list[Path] = []

    async def start(path: Path, directory: str) -> asyncio.subprocess.Process:
        script = {
            "crash": "import sys; sys.stdin.buffer.readline(); sys.stderr.write('secret-canary'); sys.exit(17)",
            "invalid-frame": "import sys; sys.stdin.buffer.readline(); print('secret-canary')",
            "backend-defect": "from structuraguard.parsers.builtin import _document_worker as worker\ndef broken_backend(*args):\n    raise RuntimeError('secret-canary')\nworker._units = broken_backend\nworker.main()",
        }[mode]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            script,
            str(path),
            cwd=directory,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(process)
        snapshots.append(path)
        return process

    monkeypatch.setattr(_documents, "_start_worker", start)
    content = zip_bytes(package_parts("docx"))
    source = source_for(content, display_name="valid.docx")
    with pytest.raises(ParserError) as failure:
        await collect(DocxParser(), source, contexts_for(source, content)[1])
    assert failure.value.error_code == "PARSER_OUTPUT_INVALID"
    assert "secret-canary" not in "".join(traceback.format_exception(failure.value))
    assert processes and all(process.returncode is not None for process in processes)
    assert all(not path.parent.exists() for path in snapshots)
