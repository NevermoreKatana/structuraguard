"""Bounded document transport, ZIP preflight и read-only OOXML primitives."""

from __future__ import annotations

import asyncio
import importlib.util
import math
import posixpath
import stat
import struct
import sys
import tempfile
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
from xml.etree.ElementTree import Element, ParseError, XMLParser

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.source import (
    ExtensionMetadataEntry,
    ExtractedBatch,
    ExtractedBlock,
    ExtractedTable,
    ExtractedValue,
    ProbeResult,
    ProbeSignalKind,
    SourceArtifact,
    SourceLocation,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.ports.source import ParseContext, ProbeContext

from ._common import _read_checked, limit_error
from ._markup import (
    Budget,
    MarkupDocument,
    MarkupLimits,
    MarkupUnit,
    byte_chunks,
    malformed,
    markup_batches,
    missing_extra,
    probe_result,
    rejected,
)

type DocumentFormat = Literal["xlsx", "pdf", "docx"]
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORKER_LIMIT_RESOURCES = frozenset(
    {
        "archive_members",
        "central_directory_bytes",
        "member_bytes",
        "extracted_bytes",
        "compression_ratio",
        "relationships",
        "xml_nodes",
        "xml_depth",
        "xml_attributes",
        "xml_name_chars",
        "xml_attribute_chars",
        "xml_text_chars",
        "source_bytes",
        "value_chars",
        "text_chars",
        "physical_objects",
        "records",
        "document_unit",
        "worker_frame_bytes",
        "sheets",
        "rows_per_sheet",
        "columns",
        "cells",
        "shared_strings",
        "merged_ranges",
        "unit_cells",
        "blocks",
        "table_cells",
        "runs_per_paragraph",
        "paragraph_chars",
        "pages",
        "pdf_objects",
        "pdf_object_chars",
        "pdf_stream_bytes",
        "pdf_decoded_stream_bytes",
        "pdf_decoded_bytes",
        "page_blocks",
        "page_tables",
        "page_cells",
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DocumentParserLimits(MarkupLimits):
    """Finite worker/spool/container caps. strict_mode требует будущий sandbox M12."""

    max_source_bytes: int = 32 * 1024 * 1024
    timeout_seconds: float = 30.0
    worker_memory_mb: int = 768
    strict_mode: bool = False
    max_members: int = 4096
    max_central_directory_bytes: int = 1024 * 1024
    max_member_bytes: int = 8 * 1024 * 1024
    max_extracted_bytes: int = 64 * 1024 * 1024
    max_compression_ratio: int = 200
    max_relationships: int = 4096
    max_xml_nodes: int = 1_000_000

    def __post_init__(self) -> None:
        MarkupLimits.__post_init__(self)
        for name, cap in (
            ("max_source_bytes", 512 * 1024 * 1024),
            ("worker_memory_mb", 4096),
            ("max_members", 10000),
            ("max_central_directory_bytes", 4 * 1024 * 1024),
            ("max_member_bytes", 64 * 1024 * 1024),
            ("max_extracted_bytes", 512 * 1024 * 1024),
            ("max_compression_ratio", 1000),
            ("max_relationships", 100000),
            ("max_xml_nodes", 10000000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= cap:
                raise ValueError(f"{name} должен быть int от 1 до {cap}")
        if type(self.strict_mode) is not bool:
            raise ValueError("strict_mode должен быть bool")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 3600
        ):
            raise ValueError("timeout_seconds должен быть в (0, 3600]")


class DocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: DocumentFormat
    source: SourceArtifact
    limits: dict[str, int | float | bool]
    max_records: int
    max_nesting_depth: int
    max_physical_objects: int
    batch_size: int = 1000


class DocumentUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocks: tuple[ExtractedBlock, ...] = ()
    tables: tuple[ExtractedTable, ...] = ()
    error_code: str | None = None
    error_resource: str | None = Field(default=None, max_length=64)
    error_limit: int | None = Field(default=None, strict=True, ge=0, le=2**63 - 1)
    reason: str | None = None
    records: int = Field(default=1, ge=0)


class DocumentBudget:
    def __init__(self, request: DocumentRequest, limits: DocumentParserLimits) -> None:
        self.request = request
        self.limits = limits
        self.adapter_id = f"builtin.{request.format}"
        self.text_chars = 0
        self.xml_nodes = 0
        self.sequence = 0

    def check(self, resource: str, value: int, maximum: int) -> None:
        if value > maximum:
            raise limit_error(
                adapter_id=self.adapter_id, resource=resource, limit=maximum
            )

    def text(self, value: str) -> str:
        self.check("value_chars", len(value), self.limits.max_value_chars)
        self.text_chars += len(value)
        self.check("text_chars", self.text_chars, self.limits.max_text_chars)
        return value

    def identity(self, prefix: str) -> str:
        self.sequence += 1
        self.check("physical_objects", self.sequence, self.request.max_physical_objects)
        return f"{prefix}-{self.sequence}"

    def value(
        self, text: str, location: SourceLocation, hint: str | None = None
    ) -> ExtractedValue:
        return ExtractedValue(
            value_id=self.identity("value"),
            raw_value=StringScalar(value=self.text(text)),
            location=location,
            technical_type_hint=hint,
        )


def metadata(
    **values: str | int | float | bool | None,
) -> tuple[ExtensionMetadataEntry, ...]:
    return tuple(
        ExtensionMetadataEntry(key=key, value=value) for key, value in values.items()
    )


def unsupported(feature: str) -> ParserError:
    return ParserError(
        error_code="PARSER_UNSUPPORTED_FEATURE",
        message="Возможность документа не поддерживается.",
        details={"feature": feature, "reason": "feature_disabled"},
    )


def _directory(
    data: bytes, *, file_size: int, budget: DocumentBudget
) -> tuple[int, int]:
    index = data.rfind(b"PK\x05\x06")
    if index < 0 or index + 22 > len(data):
        raise malformed("invalid_zip")
    disk, central_disk, count_disk, count, size, offset, comment = struct.unpack_from(
        "<4H2IH", data, index + 4
    )
    if index + 22 + comment != len(data) or disk or central_disk or count != count_disk:
        raise malformed("invalid_zip")
    if count == 65535 or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise unsupported("zip64")
    budget.check("archive_members", count, budget.limits.max_members)
    budget.check(
        "central_directory_bytes", size, budget.limits.max_central_directory_bytes
    )
    if offset + size > file_size - 22 - comment:
        raise malformed("invalid_zip")
    return offset, size


def safe_part(name: str) -> str:
    if (
        not name
        or len(name) > 1024
        or "\\" in name
        or "\x00" in name
        or name.startswith("/")
        or ":" in name
        or any(p in {"", ".", ".."} for p in name.rstrip("/").split("/"))
    ):
        raise rejected("archive_path")
    return name


def relationship_part(part: str, target: str) -> str:
    if not target or any(c in target for c in (":", "\\", "%", "?", "\x00")):
        raise rejected("relationship_target")
    path = target.split("#", 1)[0]
    resolved = posixpath.normpath(
        path.lstrip("/")
        if path.startswith("/")
        else posixpath.join(posixpath.dirname(part), path)
    )
    return safe_part(resolved)


class _XmlTarget:
    def __init__(self, budget: DocumentBudget) -> None:
        self.budget = budget
        self.stack: list[Element] = []
        self.root: Element | None = None
        self.last: Element | None = None
        self.tail = False

    def start(self, tag: str, attrs: dict[str, str]) -> None:
        self.budget.xml_nodes += 1
        self.budget.check(
            "xml_nodes", self.budget.xml_nodes, self.budget.limits.max_xml_nodes
        )
        self.budget.check(
            "xml_depth",
            len(self.stack) + 1,
            min(self.budget.limits.max_depth, self.budget.request.max_nesting_depth),
        )
        self.budget.check("xml_attributes", len(attrs), 1024)
        self.budget.check("xml_name_chars", len(tag), self.budget.limits.max_name_chars)
        for key, value in attrs.items():
            self.budget.check(
                "xml_name_chars", len(key), self.budget.limits.max_name_chars
            )
            self.budget.check(
                "xml_attribute_chars", len(value), self.budget.limits.max_value_chars
            )
        node = Element(tag, attrs)
        if self.stack:
            self.stack[-1].append(node)
        else:
            self.root = node
        self.stack.append(node)
        self.last = node
        self.tail = False

    def end(self, tag: str) -> None:
        self.last = self.stack.pop()
        self.tail = True

    def data(self, text: str) -> None:
        if self.last is None:
            return
        old = (self.last.tail if self.tail else self.last.text) or ""
        self.budget.check(
            "xml_text_chars", len(old) + len(text), self.budget.limits.max_value_chars
        )
        if self.tail:
            self.last.tail = old + text
        else:
            self.last.text = old + text

    def close(self) -> Element:
        if self.root is None:
            raise malformed("invalid_ooxml")
        return self.root


class SafePackage:
    """ZIP никогда не извлекается в filesystem; все members и relationships проверяются."""

    def __init__(self, path: str, budget: DocumentBudget) -> None:
        self.budget = budget
        with open(path, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            budget.check("source_bytes", size, budget.limits.max_source_bytes)
            stream.seek(max(0, size - 65557))
            _directory(stream.read(65557), file_size=size, budget=budget)
        self.archive = zipfile.ZipFile(path, mode="r")
        self.names: set[str] = set()
        self.relationship_count = 0
        try:
            total = 0
            for info in self.archive.infolist():
                budget.check(
                    "archive_members", len(self.names) + 1, budget.limits.max_members
                )
                name = safe_part(info.filename)
                if name in self.names or info.orig_filename != name:
                    raise rejected("archive_duplicate")
                self.names.add(name)
                mode = info.external_attr >> 16
                if stat.S_ISLNK(mode) or (
                    stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                ):
                    raise rejected("archive_special_file")
                if info.flag_bits & 1:
                    raise unsupported("encrypted_document")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise unsupported("archive_compression")
                budget.check(
                    "member_bytes", info.file_size, budget.limits.max_member_bytes
                )
                budget.check(
                    "compression_ratio",
                    info.file_size,
                    max(1, info.compress_size) * budget.limits.max_compression_ratio,
                )
                total += info.file_size
                budget.check(
                    "extracted_bytes", total, budget.limits.max_extracted_bytes
                )
                lowered = name.lower()
                if (
                    lowered.endswith((".bin", ".exe", ".dll", ".zip", ".xlsm", ".docm"))
                    or "/embeddings/" in lowered
                    or "vbaproject" in lowered
                ):
                    raise rejected("active_package_part")
            measured = 0
            for name in sorted(self.names):
                if name.endswith("/"):
                    continue
                with self.archive.open(name) as member:
                    count = 0
                    while chunk := member.read(65536):
                        if count == 0 and chunk.startswith(
                            (b"PK\x03\x04", b"MZ", b"\xd0\xcf\x11\xe0")
                        ):
                            raise rejected("nested_or_active_container")
                        count += len(chunk)
                        measured += len(chunk)
                        budget.check(
                            "member_bytes", count, budget.limits.max_member_bytes
                        )
                        budget.check(
                            "extracted_bytes",
                            measured,
                            budget.limits.max_extracted_bytes,
                        )
                if name.endswith((".xml", ".rels")):
                    root = self.xml(name)
                    if name.endswith(".rels"):
                        self._relationships(name, root)
            types = self.xml("[Content_Types].xml")
            if types.tag != f"{{{_CT}}}Types":
                raise malformed("invalid_ooxml")
            if any(
                "macroenabled" in e.get("ContentType", "").lower()
                or "vbaproject" in e.get("ContentType", "").lower()
                for e in types
            ):
                raise rejected("macro_package")
        except BaseException:
            self.archive.close()
            raise

    def close(self) -> None:
        self.archive.close()

    def xml(self, name: str) -> Element:
        try:
            from defusedxml.common import DefusedXmlException
            from defusedxml.ElementTree import DefusedXMLParser
        except ImportError:
            raise missing_extra("xml") from None
        if name not in self.names:
            raise malformed("invalid_ooxml")
        target = _XmlTarget(self.budget)
        parser: XMLParser = DefusedXMLParser(
            target=target, forbid_dtd=True, forbid_entities=True, forbid_external=True
        )
        try:
            with self.archive.open(name) as member:
                while chunk := member.read(4096):
                    parser.feed(chunk)
                parser.close()
            if target.root is None:
                raise malformed("invalid_ooxml")
            return target.root
        except DefusedXmlException:
            raise rejected("ooxml_dtd_or_entity") from None
        except (ParseError, LookupError):
            raise malformed("invalid_ooxml") from None

    def _relationships(self, name: str, root: Element) -> None:
        if root.tag != f"{{{_REL}}}Relationships":
            raise malformed("invalid_ooxml")
        self.relationship_count += len(root)
        self.budget.check(
            "relationships",
            self.relationship_count,
            self.budget.limits.max_relationships,
        )
        part = (
            ""
            if name == "_rels/.rels"
            else name.replace("/_rels/", "/").removesuffix(".rels")
        )
        ids: set[str] = set()
        for rel in root:
            if rel.get("Id") in ids:
                raise malformed("invalid_ooxml")
            ids.add(rel.get("Id", ""))
            if rel.get("TargetMode", "Internal") != "Internal":
                raise rejected("external_relationship")
            target = relationship_part(part, rel.get("Target", ""))
            if target not in self.names:
                raise malformed("invalid_ooxml")

    def relationships(self, part: str) -> dict[str, str]:
        path = (
            posixpath.join(
                posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
            )
            if part
            else "_rels/.rels"
        )
        if path not in self.names:
            return {}
        return {
            e.get("Id", ""): relationship_part(part, e.get("Target", ""))
            for e in self.xml(path)
        }

    def require_format(self, format_id: DocumentFormat) -> None:
        main = "xl/workbook.xml" if format_id == "xlsx" else "word/document.xml"
        mime = "application/vnd.openxmlformats-officedocument." + (
            "spreadsheetml.sheet.main+xml"
            if format_id == "xlsx"
            else "wordprocessingml.document.main+xml"
        )
        if main not in self.relationships("").values() or not any(
            e.get("PartName") == "/" + main and e.get("ContentType") == mime
            for e in self.xml("[Content_Types].xml")
        ):
            raise malformed("invalid_ooxml")


async def document_probe(
    source: SourceArtifact,
    context: ProbeContext,
    limits: DocumentParserLimits,
    format_id: DocumentFormat,
) -> ProbeResult:
    supported = False
    consumed = 0
    probe_limit = min(context.max_probe_bytes, limits.max_probe_bytes)

    async def read(offset: int, size: int) -> bytes:
        nonlocal consumed
        if consumed + size > probe_limit:
            return b""
        parts = bytearray()
        while len(parts) < size:
            chunk = await _read_checked(
                context.reader, offset=offset + len(parts), size=size - len(parts)
            )
            if not chunk:
                raise malformed("unexpected_eof")
            parts.extend(chunk)
            consumed += len(chunk)
            await asyncio.sleep(0)
        return bytes(parts)

    header = await read(0, min(8, source.size_bytes))
    if format_id == "pdf":
        supported = header.startswith(b"%PDF-")
    elif header.startswith(b"PK\x03\x04") and probe_limit >= 52:
        request = DocumentRequest(
            format=format_id,
            source=source,
            limits={},
            max_records=10000,
            max_nesting_depth=64,
            max_physical_objects=10000,
        )
        size = min(source.size_bytes, 65557, (probe_limit - consumed) // 2)
        tail = await read(source.size_bytes - size, size)
        if b"PK\x05\x06" not in tail and size < min(source.size_bytes, 65557):
            offset, central_size = 0, 0
        else:
            offset, central_size = _directory(
                tail,
                file_size=source.size_bytes,
                budget=DocumentBudget(request, limits),
            )
        central = await read(offset, central_size)
        if central:
            names: set[str] = set()
            index = 0
            while index < len(central):
                if central[index : index + 4] != b"PK\x01\x02" or index + 46 > len(
                    central
                ):
                    raise malformed("invalid_zip")
                flags = struct.unpack_from("<H", central, index + 8)[0]
                name_size, extra, comment = struct.unpack_from(
                    "<3H", central, index + 28
                )
                end = index + 46 + name_size + extra + comment
                if end > len(central):
                    raise malformed("invalid_zip")
                try:
                    name = central[index + 46 : index + 46 + name_size].decode(
                        "utf-8" if flags & 2048 else "cp437"
                    )
                except UnicodeDecodeError:
                    raise malformed("invalid_zip") from None
                names.add(safe_part(name))
                index = end
            main = "xl/workbook.xml" if format_id == "xlsx" else "word/document.xml"
            supported = {"[Content_Types].xml", "_rels/.rels", main}.issubset(names)
    if supported:
        module = {"pdf": "pymupdf", "xlsx": "openpyxl", "docx": "docx"}[format_id]
        if importlib.util.find_spec(module) is None or (
            format_id != "pdf" and importlib.util.find_spec("defusedxml") is None
        ):
            raise missing_extra(
                {"pdf": "pdf", "xlsx": "excel", "docx": "office"}[format_id]
            )
    return probe_result(
        source,
        adapter_id=f"builtin.{format_id}",
        supported=supported,
        format_id=format_id,
        media_types=frozenset(
            {
                {
                    "pdf": "application/pdf",
                    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                }[format_id]
            }
        ),
        extensions=frozenset({f".{format_id}"}),
        warnings=(
            "DOCUMENT_WORKER_NOT_M12_SANDBOX",
            "DOCUMENT_STRUCTURE_VALIDATED_ON_PARSE",
        ),
        signal_kind=ProbeSignalKind.SIGNATURE
        if format_id == "pdf"
        else ProbeSignalKind.INTERNAL_STRUCTURE,
    )


def worker_failure(error: ParserError | SecurityPolicyError) -> DocumentUnit:
    """Передавать только закрытое имя resource и bounded число, без raw error details."""

    resource, limit = error.details.get("resource"), error.details.get("limit")
    if (
        error.error_code == "SECURITY_LIMIT_EXCEEDED"
        and type(resource) is str
        and resource in _WORKER_LIMIT_RESOURCES
        and type(limit) is int
        and 0 <= limit <= 2**63 - 1
    ):
        return DocumentUnit(
            error_code=error.error_code, error_resource=resource, error_limit=limit
        )
    return DocumentUnit(error_code=error.error_code)


def _worker_error(unit: DocumentUnit) -> ParserError | SecurityPolicyError:
    code = unit.error_code
    if code in {
        "SECURITY_LIMIT_EXCEEDED",
        "SECURITY_INPUT_REJECTED",
        "SECURITY_SANDBOX_REQUIRED",
    }:
        if (
            code == "SECURITY_LIMIT_EXCEEDED"
            and unit.error_resource in _WORKER_LIMIT_RESOURCES
            and unit.error_limit is not None
        ):
            return SecurityPolicyError(
                error_code=code,
                message="Document parser превысил resource limit.",
                details={"resource": unit.error_resource, "limit": unit.error_limit},
            )
        return SecurityPolicyError(
            error_code=code, message="Document parser отклонил источник."
        )
    if code not in {
        "PARSER_MALFORMED_INPUT",
        "PARSER_NO_TEXT_LAYER",
        "PARSER_DEPENDENCY_UNAVAILABLE",
        "PARSER_UNSUPPORTED_FEATURE",
    }:
        code = "PARSER_OUTPUT_INVALID"
    return ParserError(
        error_code=code, message="Document parser не может обработать источник."
    )


async def _watch_memory(
    process: asyncio.subprocess.Process, limit_mb: int, failed: asyncio.Event
) -> None:
    """Darwin RSS sampling; overshoot между samples не равен OS sandbox guarantee."""

    probe: asyncio.subprocess.Process | None = None
    try:
        while process.returncode is None:
            probe = await asyncio.create_subprocess_exec(
                "/bin/ps",
                "-o",
                "rss=",
                "-p",
                str(process.pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output, _ = await probe.communicate()
            if output.strip() and int(output.strip()) > limit_mb * 1024:
                failed.set()
                with suppress(ProcessLookupError):
                    process.kill()
                return
            await asyncio.sleep(0.1)
    except (OSError, ValueError):
        with suppress(ProcessLookupError):
            process.kill()
        raise SecurityPolicyError(
            error_code="SECURITY_SANDBOX_REQUIRED",
            message="Недоступен document memory watchdog.",
        ) from None
    finally:
        if probe is not None and probe.returncode is None:
            with suppress(ProcessLookupError):
                probe.kill()
            await probe.wait()


async def _start_worker(path: Path, directory: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "structuraguard.parsers.builtin._document_worker",
        str(path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=directory,
        env={"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        limit=16 * 1024 * 1024,
    )


async def _worker_units(
    source: SourceArtifact,
    context: ParseContext,
    limits: DocumentParserLimits,
    format_id: DocumentFormat,
) -> AsyncGenerator[MarkupUnit]:
    if limits.strict_mode:
        raise SecurityPolicyError(
            error_code="SECURITY_SANDBOX_REQUIRED",
            message="Strict mode требует sandbox runner M12.",
        )
    if sys.platform not in {"darwin", "linux"}:
        raise unsupported("worker_platform")
    budget = Budget(f"builtin.{format_id}", limits, context)
    budget.check("source_bytes", source.size_bytes, limits.max_source_bytes)
    process: asyncio.subprocess.Process | None = None
    watchdog: asyncio.Task[None] | None = None
    memory_failed = asyncio.Event()
    with tempfile.TemporaryDirectory(prefix="structuraguard-document-") as directory:
        path = Path(directory) / "snapshot"
        deadline = asyncio.get_running_loop().time() + limits.timeout_seconds
        try:
            async with asyncio.timeout_at(deadline):
                with path.open("wb") as spool:
                    async for chunk in byte_chunks(source, context, budget):
                        spool.write(chunk)
                request = DocumentRequest(
                    format=format_id,
                    source=source,
                    limits=asdict(limits),
                    max_records=context.max_records,
                    max_nesting_depth=context.max_nesting_depth,
                    max_physical_objects=context.max_physical_objects,
                    batch_size=context.batch_options.batch_size,
                )
                process = await _start_worker(path, directory)
                assert process.stdin is not None and process.stdout is not None
                if sys.platform == "darwin":
                    watchdog = asyncio.create_task(
                        _watch_memory(process, limits.worker_memory_mb, memory_failed)
                    )
                process.stdin.write(request.model_dump_json().encode() + b"\n")
                await process.stdin.drain()
                process.stdin.close()
            while True:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    line = await process.stdout.readline()
                if not line:
                    break
                unit = DocumentUnit.model_validate_json(line)
                if unit.error_code:
                    raise _worker_error(unit)
                # Timer не должен отменять код consumer между anext calls.
                yield MarkupUnit((), unit.blocks, unit.tables, unit.records)
            async with asyncio.timeout_at(deadline):
                if await process.wait() != 0:
                    if memory_failed.is_set():
                        raise limit_error(
                            adapter_id=budget.adapter_id,
                            resource="worker_memory_mb",
                            limit=limits.worker_memory_mb,
                        )
                    raise ParserError(
                        error_code="PARSER_OUTPUT_INVALID",
                        message="Document worker завершился без допустимого результата.",
                    )
        except TimeoutError:
            raise ParserError(
                error_code="PROCESSING_TIMEOUT",
                message="Истёк timeout document parser.",
            ) from None
        except (ValidationError, ValueError, OSError):
            raise ParserError(
                error_code="PARSER_OUTPUT_INVALID",
                message="Некорректный transport document worker.",
            ) from None
        finally:
            try:
                if watchdog is not None:
                    watchdog.cancel()
                    with suppress(asyncio.CancelledError):
                        await watchdog
            finally:
                if process is not None:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                    if process.stdout is not None:
                        while await process.stdout.read(65536):
                            pass
                    await asyncio.shield(process.wait())


def document_batches(
    source: SourceArtifact,
    context: ParseContext,
    limits: DocumentParserLimits,
    format_id: DocumentFormat,
) -> AsyncIterator[ExtractedBatch]:
    return markup_batches(
        source,
        context,
        Budget(f"builtin.{format_id}", limits, context),
        MarkupDocument(),
        _worker_units(source, context, limits, format_id),
        unit_batch_size=1 if format_id == "xlsx" else None,
    )
