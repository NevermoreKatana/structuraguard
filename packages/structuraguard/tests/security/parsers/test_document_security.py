"""Fixture corpus для OOXML/PDF trust boundary и worker lifecycle."""

from __future__ import annotations

import asyncio
import io
import socket
import stat
import struct
import sys
import zipfile
from collections.abc import AsyncGenerator
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from tests.unit.parsers.builtin._document_fixtures import (
    package_parts,
    pdf_bytes,
    zip_bytes,
)
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for

from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    DocxParser,
    DocxParserLimits,
    PdfParser,
    PdfParserLimits,
    XlsxParser,
    XlsxParserLimits,
    _documents,
)
from structuraguard.parsers.builtin._documents import (
    DocumentBudget,
    DocumentRequest,
    SafePackage,
)
from structuraguard.ports import Parser


@pytest.mark.anyio
@pytest.mark.parametrize("format_id", ["xlsx", "docx"])
@pytest.mark.parametrize(
    "attack",
    [
        "traversal",
        "absolute",
        "backslash",
        "macro",
        "external",
        "xxe",
        "nested",
        "ratio",
        "members",
        "central",
        "text",
        "depth",
    ],
)
async def test_ooxml_security_corpus(
    format_id: Literal["xlsx", "docx"], attack: str
) -> None:
    parts = package_parts(format_id)
    if attack in {"traversal", "absolute", "backslash", "macro", "nested"}:
        name = {
            "traversal": "../secret-canary",
            "absolute": "/secret-canary",
            "backslash": "bad\\secret-canary",
            "macro": "vbaProject.bin",
            "nested": "payload.dat",
        }[attack]
        parts[name] = (
            b"PK\x03\x04secret-canary" if attack == "nested" else b"secret-canary"
        )
    elif attack == "external":
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            b'Target="',
            b'TargetMode="External" Target="https://example.invalid/secret-canary/',
        )
    elif attack == "xxe":
        parts["evil.xml"] = (
            b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///secret-canary">]><x>&e;</x>'
        )
    elif attack == "ratio":
        parts["bomb.txt"] = b"x" * 100000
    elif attack == "text":
        parts["evil.xml"] = b"<r>" + b"x" * 100 + b"</r>"
    elif attack == "depth":
        parts["evil.xml"] = b"<x>" * 20 + b"</x>" * 20
    payload = zip_bytes(parts, compressed=attack == "ratio")
    if attack in {"members", "central"}:
        data = bytearray(payload)
        offset = data.rfind(b"PK\x05\x06")
        if attack == "members":
            struct.pack_into("<HH", data, offset + 8, 5000, 5000)
        else:
            struct.pack_into("<I", data, offset + 12, 2000000)
        payload = bytes(data)
    parser = (
        XlsxParser(
            limits=XlsxParserLimits(max_value_chars=50 if attack == "text" else 1000000)
        )
        if format_id == "xlsx"
        else DocxParser(
            limits=DocxParserLimits(max_value_chars=50 if attack == "text" else 1000000)
        )
    )
    source = source_for(payload, display_name=f"attack.{format_id}")
    with pytest.raises(SecurityPolicyError) as failure:
        await collect(parser, source, contexts_for(source, payload)[1])
    assert "secret-canary" not in str(failure.value) + str(failure.value.details)


@pytest.mark.parametrize("attack", ["symlink", "duplicate", "crc"])
def test_zip_member_validation_before_backend(
    attack: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = package_parts("docx")
    buffer = io.BytesIO(zip_bytes(parts))
    with zipfile.ZipFile(buffer, "a") as archive:
        if attack == "symlink":
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "secret-canary")
        elif attack == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("word/document.xml", parts["word/document.xml"])
    content = buffer.getvalue()
    if attack == "crc":
        content = content.replace(b"cached result", b"broken result", 1)
    path = tmp_path / "fixture.docx"
    path.write_bytes(content)
    source = source_for(content, display_name=path.name)
    budget = DocumentBudget(
        DocumentRequest(
            format="docx",
            source=source,
            limits={},
            max_records=100,
            max_nesting_depth=16,
            max_physical_objects=10000,
        ),
        DocxParserLimits(),
    )

    def deny(*args: object, **kwargs: object) -> None:
        pytest.fail("OOXML preflight не должен обращаться к сети")

    monkeypatch.setattr(socket, "create_connection", deny)
    with pytest.raises((SecurityPolicyError, zipfile.BadZipFile)):
        SafePackage(str(path), budget)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "action",
    [
        "/OpenAction << /S /JavaScript /JS (secret-canary) >>",
        "/AA << /O << /S /Launch /F (secret-canary) >> >>",
        "/Open#41ction << /S /Java#53cript /JS (secret-canary) >>",
        "/Names << /EmbeddedFiles << /Names [] >> >>",
        "/XFA (secret-canary)",
    ],
)
async def test_pdf_actions_are_rejected(action: str) -> None:
    payload = pdf_bytes(action=action)
    source = source_for(payload, display_name="attack.pdf")
    with pytest.raises(SecurityPolicyError, match="SECURITY_INPUT_REJECTED") as failure:
        await collect(PdfParser(), source, contexts_for(source, payload)[1])
    assert "secret-canary" not in str(failure.value.details)


@pytest.mark.anyio
async def test_pdf_no_text_layer_survives_registry() -> None:
    content = pdf_bytes(text="")
    source = source_for(content, display_name="scan.pdf")
    probe, context = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register(PdfParser())
    async with registry.session() as session:
        selected = await session.select(source, probe)
        with pytest.raises(ParserError, match="PARSER_NO_TEXT_LAYER"):
            async for _ in selected.parse(source, context):
                pass


@pytest.mark.anyio
@pytest.mark.parametrize(
    "parser,content",
    [
        (
            XlsxParser(limits=XlsxParserLimits(max_sheets=1)),
            zip_bytes(package_parts("xlsx")),
        ),
        (
            XlsxParser(limits=XlsxParserLimits(max_rows_per_sheet=3)),
            zip_bytes(package_parts("xlsx")),
        ),
        (
            XlsxParser(limits=XlsxParserLimits(max_columns=3)),
            zip_bytes(package_parts("xlsx")),
        ),
        (
            XlsxParser(limits=XlsxParserLimits(max_cells=7)),
            zip_bytes(package_parts("xlsx")),
        ),
        (
            DocxParser(limits=DocxParserLimits(max_blocks=1)),
            zip_bytes(package_parts("docx")),
        ),
        (
            DocxParser(limits=DocxParserLimits(max_table_cells=1)),
            zip_bytes(package_parts("docx")),
        ),
        (
            DocxParser(limits=DocxParserLimits(max_member_bytes=200)),
            zip_bytes(package_parts("docx")),
        ),
        (
            DocxParser(limits=DocxParserLimits(max_extracted_bytes=1000)),
            zip_bytes(package_parts("docx")),
        ),
        (PdfParser(limits=PdfParserLimits(max_pages=1)), pdf_bytes(pages=2)),
        (PdfParser(limits=PdfParserLimits(max_objects=3)), pdf_bytes()),
        (PdfParser(limits=PdfParserLimits(max_text_chars=3)), pdf_bytes()),
    ],
    ids=[
        "sheets",
        "rows",
        "columns",
        "cells",
        "blocks",
        "table_cells",
        "member",
        "expanded",
        "pages",
        "objects",
        "text",
    ],
)
async def test_document_limits(parser: Parser, content: bytes) -> None:
    source = source_for(content, display_name="a.bin")
    with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
        await collect(parser, source, contexts_for(source, content)[1])


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["close", "cancel", "timeout"])
async def test_worker_lifecycle(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    original = _documents._start_worker
    processes: list[asyncio.subprocess.Process] = []
    paths: list[Path] = []
    started = asyncio.Event()

    async def start(path: Path, directory: str) -> asyncio.subprocess.Process:
        if mode == "close":
            process = await original(path, directory)
        else:
            # Контролируемый worker ждёт сигнал, hard timeout обязан остановить его.
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import signal; signal.pause()",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
        processes.append(process)
        paths.append(path)
        started.set()
        return process

    monkeypatch.setattr(_documents, "_start_worker", start)
    content = zip_bytes(package_parts("docx"))
    source = source_for(content, display_name="a.docx")
    context = contexts_for(source, content, batch_size=1)[1]
    parser = DocxParser(
        limits=DocxParserLimits(timeout_seconds=0.5 if mode == "timeout" else 30)
    )
    if mode == "close":
        stream = parser.parse(source, context)
        assert isinstance(stream, AsyncGenerator)
        await anext(stream)
        await stream.aclose()
    else:
        task = asyncio.create_task(collect(parser, source, context))
        await started.wait()
        if mode == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ParserError, match="PROCESSING_TIMEOUT"):
                await task
    assert processes and all(p.returncode is not None for p in processes)
    assert all(not p.parent.exists() for p in paths)


@pytest.mark.anyio
async def test_strict_mode_is_fail_closed_before_read() -> None:
    content = pdf_bytes()
    source = source_for(content, display_name="a.pdf")
    context = replace(contexts_for(source, content)[1], max_bytes=1)
    with pytest.raises(SecurityPolicyError, match="SECURITY_SANDBOX_REQUIRED"):
        await collect(
            PdfParser(limits=PdfParserLimits(strict_mode=True)), source, context
        )


@pytest.mark.anyio
async def test_deadline_does_not_cancel_consumer_between_batches() -> None:
    content = zip_bytes(package_parts("docx"))
    source = source_for(content, display_name="a.docx")
    parser = DocxParser(limits=DocxParserLimits(timeout_seconds=2))
    stream = parser.parse(source, contexts_for(source, content, batch_size=1)[1])
    assert isinstance(stream, AsyncGenerator)
    try:
        await anext(stream)
        await asyncio.sleep(2.1)
        with pytest.raises(ParserError, match="PROCESSING_TIMEOUT"):
            await anext(stream)
    finally:
        await stream.aclose()
