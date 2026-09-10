#!/usr/bin/env python3
"""Проверить wheel и sdist StructuraGuard без обращения к сети."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import tomllib
import venv
import zipfile
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import cast

_DISTRIBUTION_NAME = "structuraguard"
_VERSION = "0.3.0"
_WHEEL_NAME = f"{_DISTRIBUTION_NAME}-{_VERSION}-py3-none-any.whl"
_SDIST_NAME = f"{_DISTRIBUTION_NAME}-{_VERSION}.tar.gz"
_DIST_INFO = f"{_DISTRIBUTION_NAME}-{_VERSION}.dist-info"
_SDIST_ROOT = f"{_DISTRIBUTION_NAME}-{_VERSION}"

_PACKAGE_FILES = frozenset(
    {
        "structuraguard/__init__.py",
        "structuraguard/config.py",
        "structuraguard/contracts/__init__.py",
        "structuraguard/contracts/_base.py",
        "structuraguard/contracts/analysis.py",
        "structuraguard/contracts/common.py",
        "structuraguard/contracts/database.py",
        "structuraguard/contracts/execution.py",
        "structuraguard/contracts/mapping.py",
        "structuraguard/contracts/normalized.py",
        "structuraguard/contracts/parsing.py",
        "structuraguard/contracts/plugins.py",
        "structuraguard/contracts/reports.py",
        "structuraguard/contracts/source.py",
        "structuraguard/contracts/structure.py",
        "structuraguard/domain/__init__.py",
        "structuraguard/domain/canonical.py",
        "structuraguard/domain/lineage.py",
        "structuraguard/exceptions.py",
        "structuraguard/structure/__init__.py",
        "structuraguard/structure/_observations.py",
        "structuraguard/structure/_plan_check.py",
        "structuraguard/structure/_runtime.py",
        "structuraguard/structure/_samples.py",
        "structuraguard/structure/_stream.py",
        "structuraguard/structure/analysis.py",
        "structuraguard/structure/document.py",
        "structuraguard/structure/execution.py",
        "structuraguard/structure/planning.py",
        "structuraguard/structure/profiling.py",
        "structuraguard/structure/tabular.py",
        "structuraguard/structure/text.py",
        "structuraguard/structure/tree.py",
        "structuraguard/structure/validation.py",
        "structuraguard/parsers/__init__.py",
        "structuraguard/parsers/_hashing.py",
        "structuraguard/parsers/_registration.py",
        "structuraguard/parsers/builtin/__init__.py",
        "structuraguard/parsers/builtin/_common.py",
        "structuraguard/parsers/builtin/_json.py",
        "structuraguard/parsers/builtin/_markup.py",
        "structuraguard/parsers/tika.py",
        "structuraguard/parsers/_tika_http.py",
        "structuraguard/parsers/builtin/_documents.py",
        "structuraguard/parsers/builtin/_document_worker.py",
        "structuraguard/parsers/builtin/xlsx.py",
        "structuraguard/parsers/builtin/pdf.py",
        "structuraguard/parsers/builtin/docx.py",
        "structuraguard/parsers/builtin/xml.py",
        "structuraguard/parsers/builtin/html.py",
        "structuraguard/parsers/builtin/yaml.py",
        "structuraguard/parsers/builtin/delimited.py",
        "structuraguard/parsers/builtin/json_document.py",
        "structuraguard/parsers/builtin/json_lines.py",
        "structuraguard/parsers/builtin/log.py",
        "structuraguard/parsers/builtin/markdown.py",
        "structuraguard/parsers/builtin/text.py",
        "structuraguard/parsers/discovery.py",
        "structuraguard/parsers/execution.py",
        "structuraguard/parsers/registry.py",
        "structuraguard/parsers/selection.py",
        "structuraguard/ports/__init__.py",
        "structuraguard/ports/database.py",
        "structuraguard/ports/llm.py",
        "structuraguard/ports/parser.py",
        "structuraguard/ports/security.py",
        "structuraguard/ports/semantic.py",
        "structuraguard/ports/source.py",
        "structuraguard/ports/stores.py",
        "structuraguard/py.typed",
        "structuraguard/sdk.py",
        "structuraguard/sync_sdk.py",
    }
)
_PUBLIC_EXPORTS = frozenset(
    {
        "SDKConfig",
        "AsyncStructuraGuard",
        "StructuraGuard",
        "StructuraGuardError",
        "OperationNotImplementedError",
        "SourceError",
        "ParserError",
        "DatabaseInspectionError",
        "MappingError",
        "ValidationError",
        "SecurityPolicyError",
        "LoadError",
    }
)
_CONTRACT_EXPORTS = frozenset(
    {
        "AuditEvent",
        "BatchFingerprint",
        "BooleanScalar",
        "BoundingBox",
        "BuiltInErrorCode",
        "BuiltInIssueCode",
        "BytesScalar",
        "CatalogColumnRef",
        "ColumnCatalog",
        "CssSelectorLocation",
        "DatabaseCatalog",
        "DatabaseInspectionRequest",
        "DataClassification",
        "DateScalar",
        "DateTimeScalar",
        "DecimalScalar",
        "DocumentBlockLocation",
        "DocumentBlockGrouping",
        "DocumentParsePlan",
        "DocumentShapeObservation",
        "DocumentTargetSelector",
        "EveryLineStart",
        "ErrorPolicy",
        "ExtensionLocation",
        "ExtensionMetadataEntry",
        "ExtractedBatch",
        "ExtractedBatchSummary",
        "ExtractedBlock",
        "ExtractedBlockKind",
        "ExtractedCell",
        "ExtractedDatasetManifest",
        "ExtractedLine",
        "ExtractedSourceIndex",
        "ExtractedTable",
        "ExtractedTreeNode",
        "ExtractedValue",
        "FieldMapping",
        "ForeignKeyCatalog",
        "GroupLinesRule",
        "IntegerScalar",
        "IssueSeverity",
        "JsonPointerLocation",
        "LLMRequest",
        "LLMResponse",
        "LineRangeLocation",
        "LoadContext",
        "LoadOperation",
        "LoadPolicy",
        "LoadReport",
        "LogParsePlan",
        "LogLineGrouping",
        "LogShapeObservation",
        "LogTokenSelector",
        "LogRecordStart",
        "MappingCandidate",
        "MappingPlan",
        "MappingPlanValidationRequest",
        "MappingPlanValidationResult",
        "MappingPolicyRef",
        "NormalizedBatch",
        "NormalizedBatchSummary",
        "NormalizedDatasetManifest",
        "NormalizedRecord",
        "NormalizedScalar",
        "NormalizedValue",
        "NullScalar",
        "NumberScalar",
        "ParseExecutionContext",
        "ParseEntity",
        "ParseEntityGrouping",
        "ParseField",
        "ParseFieldSelector",
        "ParsePlan",
        "ParsePlanKind",
        "ParsePlanValidationRequest",
        "ParsePlanValidationResult",
        "ParseRule",
        "PARSER_ENTRY_POINT_GROUP",
        "ParserDiscoveryPolicy",
        "ParserPluginDescriptor",
        "PhysicalMetadataEntry",
        "PhysicalNodeKind",
        "PhysicalObjectKind",
        "PhysicalSample",
        "PhysicalSourceRef",
        "PipelineStatus",
        "ProbeResult",
        "ProbeSignal",
        "ProbeSignalKind",
        "ProbeSignalOutcome",
        "ProducerMetadata",
        "PrefixTokenStart",
        "ProviderCapabilities",
        "RawScalar",
        "RecordRangeRule",
        "SchemaCatalog",
        "SecurityApproval",
        "SecurityReport",
        "SecurityScanRequest",
        "SemanticEntity",
        "SemanticField",
        "SemanticFieldRef",
        "SemanticParseReport",
        "SemanticParsingMode",
        "SemanticSourceIndex",
        "SheetCellLocation",
        "SourceArtifact",
        "SourceArtifactRef",
        "SourceLocation",
        "StagingContext",
        "StringScalar",
        "StructureAnalysisRequest",
        "StructureAnalysisResult",
        "StructureCandidate",
        "StructureEvidence",
        "StructureNeedsReview",
        "StructureObservation",
        "StructurePlanCreated",
        "StructureProfile",
        "StructureRejected",
        "TableCatalog",
        "TabularCellLocation",
        "TabularColumnSelector",
        "TabularParsePlan",
        "TabularRowGrouping",
        "TabularShapeObservation",
        "TransactionOutcome",
        "TreeParsePlan",
        "TreeNodeGrouping",
        "TreePathSelector",
        "TreeShapeObservation",
        "ValidatedMappingPlan",
        "ValidatedParsePlan",
        "ValidationDecision",
        "ValidationIssue",
        "ValidationReport",
        "XPathLocation",
    }
)
_DOMAIN_EXPORTS = frozenset(
    {
        "batch_sequence_is_contiguous",
        "canonical_json",
        "references_belong_to_extraction",
        "sha256_fingerprint",
        "unresolved_physical_refs",
    }
)
_PORT_EXPORTS = frozenset(
    {
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
_STRUCTURE_EXPORTS = frozenset(
    {
        "DeterministicStructureAnalyzer",
        "ParsePlanExecutor",
        "ParsePlanOptions",
        "ParsePlanValidator",
        "StructuralProfiler",
        "StructuralProfilingOptions",
        "StructureAnalysisOptions",
    }
)
_PARSER_EXPORTS = frozenset(
    {
        "PARSER_ENTRY_POINT_GROUP",
        "ParserIdentity",
        "ParserPluginDiscoveryFailure",
        "ParserPluginDiscoveryReport",
        "ParserRegistry",
        "ParserRegistrySession",
        "ParserRegistrySnapshot",
        "SelectedParser",
        "ValidatedParserStream",
        "discover_parser_plugins",
    }
)
_BUILTIN_PARSER_EXPORTS = frozenset(
    {
        "XlsxParser",
        "XlsxParserLimits",
        "PdfParser",
        "PdfParserLimits",
        "DocxParser",
        "DocxParserLimits",
        "builtin_document_parsers",
        "XmlParser",
        "XmlParserLimits",
        "HtmlParser",
        "HtmlParserLimits",
        "YamlParser",
        "YamlParserLimits",
        "builtin_markup_parsers",
        "html_safe_json",
        "DelimitedDetectionOptions",
        "DelimitedDialect",
        "DelimitedParserLimits",
        "DelimitedTextParser",
        "JsonDocumentParser",
        "JsonLinesParser",
        "JsonParserLimits",
        "LogParser",
        "LogParserLimits",
        "MarkdownParser",
        "MarkdownParserLimits",
        "PlainTextParser",
        "TextParserLimits",
        "builtin_delimited_parsers",
        "builtin_json_parsers",
        "builtin_text_parsers",
    }
)
_OPTIONAL_DEPENDENCIES = {
    "xml": frozenset({"defusedxml"}),
    "yaml": frozenset({"pyyaml"}),
    "postgres": frozenset({"sqlalchemy", "asyncpg", "psycopg"}),
    "pdf": frozenset({"pymupdf"}),
    "excel": frozenset({"openpyxl", "defusedxml"}),
    "office": frozenset({"python-docx", "defusedxml"}),
    "litellm": frozenset({"litellm"}),
    "tika": frozenset({"httpx", "httpcore", "defusedxml"}),
}
_EXPECTED_EXTRAS = frozenset({*_OPTIONAL_DEPENDENCIES, "all"})
_FORBIDDEN_DEPENDENCIES = frozenset(
    {
        "celery",
        "django",
        "fastapi",
        "flask",
        "litestar",
        "quart",
        "redis",
        "sanic",
        "starlette",
        "tornado",
    }
)
_FORBIDDEN_OPTIONAL_IMPORTS = frozenset(
    {
        "httpx",
        "httpcore",
        "defusedxml",
        "yaml",
        "asyncpg",
        "docx",
        "fitz",
        "litellm",
        "openpyxl",
        "psycopg",
        "pymupdf",
        "sqlalchemy",
        "tika",
    }
)
_CORE_RUNTIME_CLOSURE = frozenset(
    {
        "annotated-types",
        "charset-normalizer",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    }
)
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_EXTRA_MARKER = re.compile(r"\bextra\s*==\s*(['\"])([A-Za-z0-9._-]+)\1")
_MAX_ARCHIVE_SIZE = 50 * 1024 * 1024
_MAX_UNCOMPRESSED_SIZE = 30 * 1024 * 1024
_MAX_TAR_STREAM_SIZE = 32 * 1024 * 1024
_MAX_MEMBER_SIZE = 20 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 512
_TAR_BLOCK_SIZE = 512
_ALLOWED_TAR_TYPES = frozenset({b"\0", b"0", b"5"})
_COMMAND_TIMEOUT_SECONDS = 180
_ALLOWED_SUBPROCESS_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "UV_CACHE_DIR",
        "WINDIR",
        "XDG_CACHE_HOME",
    }
)


class VerificationError(RuntimeError):
    """Контролируемая ошибка проверки distribution."""


class _BoundedDecompressedReader(io.RawIOBase):
    """Ограничить весь tar stream, включая скрытые extended headers."""

    def __init__(self, stream: gzip.GzipFile, *, limit: int) -> None:
        super().__init__()
        self._stream = stream
        self._limit = limit
        self._remaining = limit

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        requested = self._remaining + 1 if size < 0 else min(size, self._remaining + 1)
        payload = self._stream.read(requested)
        if len(payload) > self._remaining:
            raise _fail(f"Decompressed tar stream превышает лимит {self._limit} bytes")
        self._remaining -= len(payload)
        return payload


@dataclass(frozen=True)
class MetadataContract:
    """Значимые для M1 поля core metadata."""

    version: str
    requires_python: str
    requires_dist: tuple[str, ...]
    provides_extra: tuple[str, ...]
    runtime_requirements: tuple[str, ...]


@dataclass(frozen=True)
class WheelInspection:
    """Проверенные данные wheel, используемые для сравнения rebuild."""

    metadata: MetadataContract
    package_payloads: dict[str, bytes]
    archive_files: frozenset[str]


@dataclass(frozen=True)
class SdistInspection:
    """Проверенные данные sdist, используемые для сравнения с wheel."""

    metadata: MetadataContract
    package_payloads: dict[str, bytes]
    pyproject_payload: bytes
    gitignore_payload: bytes


def _fail(message: str) -> VerificationError:
    return VerificationError(message)


def _read_exact(
    stream: _BoundedDecompressedReader,
    size: int,
    *,
    source: str,
) -> bytes:
    payload = bytearray()
    while len(payload) < size:
        chunk = stream.read(size - len(payload))
        if not chunk:
            raise _fail(f"{source}: tar stream неожиданно завершился")
        payload.extend(chunk)
    return bytes(payload)


def _discard_exact(
    stream: _BoundedDecompressedReader,
    size: int,
    *,
    source: str,
) -> None:
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            raise _fail(f"{source}: tar member неожиданно завершился")
        remaining -= len(chunk)


def _tar_octal(field: bytes, *, source: str, label: str) -> int:
    stripped = field.strip(b" \0")
    if not stripped:
        return 0
    if any(byte not in b"01234567" for byte in stripped):
        raise _fail(f"{source}: некорректное octal-поле tar {label}")
    return int(stripped, 8)


def _validate_plain_tar_structure(
    stream: _BoundedDecompressedReader,
    *,
    source: str,
) -> None:
    member_count = 0
    total_size = 0
    while True:
        header = _read_exact(stream, _TAR_BLOCK_SIZE, source=source)
        if header == bytes(_TAR_BLOCK_SIZE):
            second_zero_block = _read_exact(stream, _TAR_BLOCK_SIZE, source=source)
            if second_zero_block != bytes(_TAR_BLOCK_SIZE):
                raise _fail(f"{source}: tar должен завершаться двумя zero blocks")
            while trailing := stream.read(64 * 1024):
                if any(trailing):
                    raise _fail(f"{source}: non-zero data после tar terminator")
            return

        stored_checksum = _tar_octal(
            header[148:156],
            source=source,
            label="checksum",
        )
        calculated_checksum = sum(header[:148]) + (8 * ord(" ")) + sum(header[156:])
        if stored_checksum != calculated_checksum:
            raise _fail(f"{source}: неверная checksum tar header")

        member_type = header[156:157]
        if member_type not in _ALLOWED_TAR_TYPES:
            raise _fail(
                f"{source}: extended и special tar headers запрещены: "
                f"type={member_type!r}"
            )
        member_count += 1
        if member_count > _MAX_ARCHIVE_MEMBERS:
            raise _fail(f"{source}: слишком много archive members")

        member_size = _tar_octal(header[124:136], source=source, label="size")
        if member_type == b"5" and member_size:
            raise _fail(f"{source}: directory tar member должен иметь нулевой size")
        if member_size > _MAX_MEMBER_SIZE:
            raise _fail(f"{source}: member превышает лимит")
        total_size += member_size
        if total_size > _MAX_UNCOMPRESSED_SIZE:
            raise _fail(f"{source}: uncompressed contents превышают лимит")
        padding = (-member_size) % _TAR_BLOCK_SIZE
        _discard_exact(stream, member_size + padding, source=source)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(requirement: str) -> str:
    match = _REQUIREMENT_NAME.match(requirement)
    if match is None:
        raise _fail(f"Некорректный Requires-Dist: {requirement!r}")
    return _normalize_name(match.group(1))


def _canonical_requirement(requirement: str) -> str:
    base = requirement.partition(";")[0].strip()
    if "@" in base:
        raise _fail(f"Direct URL dependency запрещена: {requirement!r}")
    match = _REQUIREMENT_NAME.match(base)
    if match is None:
        raise _fail(f"Некорректная dependency: {requirement!r}")
    suffix = re.sub(r"\s+", "", base[match.end() :]).lower()
    return f"{_normalize_name(match.group(1))}{suffix}"


def _is_pydantic_v2(requirement: str) -> bool:
    compact = _canonical_requirement(requirement)
    if not compact.startswith("pydantic"):
        return False
    version_spec = compact.removeprefix("pydantic")
    compatible_release = re.search(r"~=2(?:\.|$)", version_spec) is not None
    exact_v2 = re.search(r"==2(?:\.|\*|$)", version_spec) is not None
    bounded_v2 = (
        re.search(r">=2(?:\.|,|$)", version_spec) is not None
        and re.search(r"<3(?:\.|,|$)", version_spec) is not None
    )
    return compatible_release or exact_v2 or bounded_v2


def _is_charset_normalizer_v3(requirement: str) -> bool:
    compact = _canonical_requirement(requirement)
    package_name = "charset-normalizer"
    if not compact.startswith(package_name):
        return False
    version_spec = compact.removeprefix(package_name)
    return set(version_spec.split(",")) == {">=3.4", "<4"}


def _metadata_value(message: Message, field: str) -> str:
    values = message.get_all(field, [])
    if len(values) != 1:
        raise _fail(f"Core metadata должна содержать ровно одно поле {field}")
    return str(values[0]).strip()


def _parse_metadata(payload: bytes, *, source: str) -> MetadataContract:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    name = _metadata_value(message, "Name")
    version = _metadata_value(message, "Version")
    requires_python = _metadata_value(message, "Requires-Python")

    if _normalize_name(name) != _DISTRIBUTION_NAME:
        raise _fail(f"{source}: неожиданное distribution name {name!r}")
    if version != _VERSION:
        raise _fail(f"{source}: ожидалась версия {_VERSION}, получена {version}")
    if re.sub(r"\s+", "", requires_python) != ">=3.12":
        raise _fail(f"{source}: Requires-Python должен быть ровно >=3.12")

    requires_dist = tuple(
        str(value).strip() for value in message.get_all("Requires-Dist", [])
    )
    provides_extra = tuple(
        _normalize_name(str(value).strip())
        for value in message.get_all("Provides-Extra", [])
    )
    runtime_requirements = _validate_dependency_contract(
        requires_dist,
        provides_extra,
        source=source,
    )
    return MetadataContract(
        version=version,
        requires_python=requires_python,
        requires_dist=tuple(sorted(requires_dist)),
        provides_extra=tuple(sorted(provides_extra)),
        runtime_requirements=runtime_requirements,
    )


def _validate_dependency_contract(
    requirements: tuple[str, ...],
    provides_extra: tuple[str, ...],
    *,
    source: str,
) -> tuple[str, ...]:
    if len(provides_extra) != len(set(provides_extra)):
        raise _fail(f"{source}: Provides-Extra содержит дубликаты")
    if set(provides_extra) != _EXPECTED_EXTRAS:
        raise _fail(
            f"{source}: extras должны быть {sorted(_EXPECTED_EXTRAS)}, "
            f"получены {sorted(provides_extra)}"
        )

    runtime: list[str] = []
    by_extra: dict[str, list[str]] = {extra: [] for extra in _EXPECTED_EXTRAS}
    for requirement in requirements:
        dependency_name = _requirement_name(requirement)
        if dependency_name in _FORBIDDEN_DEPENDENCIES:
            raise _fail(f"{source}: framework dependency запрещена: {dependency_name}")

        _, separator, marker = requirement.partition(";")
        extras = [match[1] for match in _EXTRA_MARKER.findall(marker)]
        normalized_extras = [_normalize_name(extra) for extra in extras]
        if not separator:
            runtime.append(requirement)
            continue
        if "extra" in marker.lower() and not normalized_extras:
            raise _fail(f"{source}: неподдерживаемый extra marker: {requirement!r}")
        if not normalized_extras:
            raise _fail(
                f"{source}: условная base dependency вне extra запрещена: {requirement!r}"
            )
        for extra in normalized_extras:
            if extra not in by_extra:
                raise _fail(f"{source}: неизвестный extra {extra!r}")
            by_extra[extra].append(requirement)

    runtime_by_name = {_requirement_name(item): item for item in runtime}
    if len(runtime_by_name) != len(runtime):
        raise _fail(f"{source}: base dependencies содержат дубликаты")
    if set(runtime_by_name) != {"charset-normalizer", "pydantic"}:
        raise _fail(
            f"{source}: base dependencies должны быть ровно "
            "Pydantic v2 и charset-normalizer v3"
        )
    if not _is_pydantic_v2(runtime_by_name["pydantic"]):
        raise _fail(f"{source}: Pydantic должен быть ограничен major-версией 2")
    if not _is_charset_normalizer_v3(runtime_by_name["charset-normalizer"]):
        raise _fail(
            f"{source}: charset-normalizer должен быть ограничен диапазоном >=3.4,<4"
        )

    canonical_by_extra: dict[str, set[str]] = {}
    for extra, extra_requirements in by_extra.items():
        canonical = [_canonical_requirement(item) for item in extra_requirements]
        if len(canonical) != len(set(canonical)):
            raise _fail(f"{source}: extra {extra!r} содержит duplicate dependencies")
        canonical_by_extra[extra] = set(canonical)
        dependency_names = {_requirement_name(item) for item in extra_requirements}
        if extra != "all" and dependency_names != _OPTIONAL_DEPENDENCIES[extra]:
            raise _fail(
                f"{source}: unexpected dependencies extra {extra!r}: "
                f"{sorted(dependency_names)}"
            )

    expected_all: set[str] = set()
    for extra in _OPTIONAL_DEPENDENCIES:
        expected_all.update(canonical_by_extra[extra])
    if canonical_by_extra["all"] != expected_all:
        raise _fail(f"{source}: extra 'all' не равен объединению остальных extras")
    return tuple(sorted(item.partition(";")[0].strip() for item in runtime))


def _safe_archive_path(name: str, *, source: str) -> PurePosixPath:
    if "\\" in name or "\x00" in name:
        raise _fail(f"{source}: небезопасное имя archive member {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise _fail(f"{source}: path traversal в archive member {name!r}")
    return path


def _validate_no_forbidden_files(
    paths: set[str],
    *,
    source: str,
    allowed_gitignore: str | None = None,
) -> None:
    forbidden_roots = {".agents", ".codex", ".git", ".github", "docs", "scripts"}
    forbidden_names = {
        ".coverage",
        ".ds_store",
        ".env",
        ".gitignore",
        "agents.md",
        "makefile",
        "mkdocs.yml",
        "uv.lock",
    }
    secret_suffixes = (".key", ".p12", ".pfx", ".pem")
    for raw_path in paths:
        parts = PurePosixPath(raw_path).parts
        lowered = tuple(part.lower() for part in parts)
        basename = lowered[-1]
        if any(part in forbidden_roots for part in lowered):
            raise _fail(f"{source}: repository-only path попал в artifact: {raw_path}")
        if "tests" in lowered or ".pytest_cache" in lowered:
            raise _fail(f"{source}: test artifact попал в distribution: {raw_path}")
        if "__pycache__" in lowered or basename.endswith((".pyc", ".pyo")):
            raise _fail(f"{source}: Python cache попал в distribution: {raw_path}")
        forbidden_name = basename in forbidden_names and raw_path != allowed_gitignore
        if forbidden_name or basename.startswith(".env."):
            raise _fail(f"{source}: запрещённый файл попал в artifact: {raw_path}")
        if basename.startswith("test_") or basename.endswith("_test.py"):
            raise _fail(f"{source}: test module попал в distribution: {raw_path}")
        if basename.endswith(secret_suffixes) or basename in {"id_ed25519", "id_rsa"}:
            raise _fail(f"{source}: возможный secret попал в distribution: {raw_path}")


def _read_wheel(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > _MAX_ARCHIVE_SIZE:
        raise _fail(f"Wheel превышает лимит {_MAX_ARCHIVE_SIZE} bytes: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            payloads: dict[str, bytes] = {}
            total_size = 0
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise _fail(f"{path.name}: слишком много archive members")
            for info in members:
                safe_path = _safe_archive_path(info.filename, source=path.name)
                name = safe_path.as_posix()
                if info.flag_bits & 0x1:
                    raise _fail(f"{path.name}: encrypted member запрещён: {name}")
                file_type = (info.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise _fail(f"{path.name}: symlink member запрещён: {name}")
                if info.is_dir():
                    continue
                if file_type not in {0, stat.S_IFREG}:
                    raise _fail(f"{path.name}: special-file member запрещён: {name}")
                if info.file_size > _MAX_MEMBER_SIZE:
                    raise _fail(f"{path.name}: member превышает лимит: {name}")
                if name in payloads:
                    raise _fail(f"{path.name}: duplicate archive member: {name}")
                total_size += info.file_size
                if total_size > _MAX_UNCOMPRESSED_SIZE:
                    raise _fail(f"{path.name}: uncompressed contents превышают лимит")
                payloads[name] = archive.read(info)
    except zipfile.BadZipFile as error:
        raise _fail(f"Некорректный wheel {path}: {error}") from error
    return payloads


def _verify_record(payloads: dict[str, bytes], *, wheel_name: str) -> None:
    record_path = f"{_DIST_INFO}/RECORD"
    record_payload = payloads.get(record_path)
    if record_payload is None:
        raise _fail(f"{wheel_name}: отсутствует {_DIST_INFO}/RECORD")
    try:
        rows = tuple(csv.reader(io.StringIO(record_payload.decode("utf-8"))))
    except UnicodeDecodeError as error:
        raise _fail(f"{wheel_name}: RECORD должен быть UTF-8") from error

    recorded: set[str] = set()
    for row in rows:
        if len(row) != 3:
            raise _fail(f"{wheel_name}: каждая строка RECORD должна иметь три поля")
        raw_name, digest_field, size_field = row
        name = _safe_archive_path(raw_name, source=f"{wheel_name}: RECORD").as_posix()
        if name in recorded:
            raise _fail(f"{wheel_name}: duplicate RECORD entry: {name}")
        recorded.add(name)
        payload = payloads.get(name)
        if payload is None:
            raise _fail(f"{wheel_name}: RECORD ссылается на отсутствующий файл {name}")
        if name == record_path:
            if digest_field or size_field:
                raise _fail(
                    f"{wheel_name}: собственная RECORD entry должна быть без hash/size"
                )
            continue
        if not digest_field.startswith("sha256="):
            raise _fail(f"{wheel_name}: для {name} требуется sha256 в RECORD")
        expected_digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest())
        expected_digest_text = expected_digest.rstrip(b"=").decode("ascii")
        if digest_field.removeprefix("sha256=") != expected_digest_text:
            raise _fail(f"{wheel_name}: неверный RECORD hash для {name}")
        if size_field != str(len(payload)):
            raise _fail(f"{wheel_name}: неверный RECORD size для {name}")
    if recorded != set(payloads):
        missing = sorted(set(payloads) - recorded)
        raise _fail(f"{wheel_name}: файлы отсутствуют в RECORD: {missing}")


def _verify_wheel(path: Path) -> WheelInspection:
    if path.name != _WHEEL_NAME:
        raise _fail(f"Wheel должен называться {_WHEEL_NAME}, получен {path.name}")
    payloads = _read_wheel(path)
    paths = set(payloads)
    _validate_no_forbidden_files(paths, source=path.name)

    metadata_path = f"{_DIST_INFO}/METADATA"
    wheel_metadata_path = f"{_DIST_INFO}/WHEEL"
    required_dist_info = {metadata_path, wheel_metadata_path, f"{_DIST_INFO}/RECORD"}
    if not required_dist_info.issubset(paths):
        missing = sorted(required_dist_info - paths)
        raise _fail(f"{path.name}: отсутствуют обязательные wheel files: {missing}")

    unexpected: set[str] = set()
    for archive_path in paths:
        if archive_path in _PACKAGE_FILES or archive_path in required_dist_info:
            continue
        if archive_path.startswith(f"{_DIST_INFO}/licenses/"):
            continue
        unexpected.add(archive_path)
    if unexpected:
        raise _fail(f"{path.name}: неожиданные wheel contents: {sorted(unexpected)}")
    if not _PACKAGE_FILES.issubset(paths):
        missing = sorted(_PACKAGE_FILES - paths)
        raise _fail(f"{path.name}: отсутствуют package files: {missing}")

    _verify_record(payloads, wheel_name=path.name)
    wheel_message = BytesParser(policy=policy.default).parsebytes(
        payloads[wheel_metadata_path]
    )
    if _metadata_value(wheel_message, "Root-Is-Purelib").lower() != "true":
        raise _fail(f"{path.name}: wheel должен быть purelib")
    tags = {str(value).strip() for value in wheel_message.get_all("Tag", [])}
    if tags != {"py3-none-any"}:
        raise _fail(f"{path.name}: wheel tag должен быть py3-none-any, получено {tags}")

    metadata = _parse_metadata(payloads[metadata_path], source=path.name)
    package_payloads = {name: payloads[name] for name in _PACKAGE_FILES}
    return WheelInspection(
        metadata=metadata,
        package_payloads=package_payloads,
        archive_files=frozenset(paths),
    )


def _read_sdist(path: Path) -> dict[str, bytes]:
    if path.stat().st_size > _MAX_ARCHIVE_SIZE:
        raise _fail(f"Sdist превышает лимит {_MAX_ARCHIVE_SIZE} bytes: {path}")
    try:
        with (
            path.open("rb") as compressed_stream,
            gzip.GzipFile(fileobj=compressed_stream, mode="rb") as gzip_stream,
        ):
            validation_stream = _BoundedDecompressedReader(
                gzip_stream,
                limit=_MAX_TAR_STREAM_SIZE,
            )
            _validate_plain_tar_structure(validation_stream, source=path.name)
            gzip_stream.seek(0)
            bounded_stream = _BoundedDecompressedReader(
                gzip_stream,
                limit=_MAX_TAR_STREAM_SIZE,
            )
            with tarfile.open(fileobj=bounded_stream, mode="r|") as archive:
                payloads: dict[str, bytes] = {}
                total_size = 0
                for member_index, member in enumerate(archive, start=1):
                    if member_index > _MAX_ARCHIVE_MEMBERS:
                        raise _fail(f"{path.name}: слишком много archive members")
                    safe_path = _safe_archive_path(member.name, source=path.name)
                    name = safe_path.as_posix()
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise _fail(
                            f"{path.name}: разрешены только regular files: {name}"
                        )
                    if member.size > _MAX_MEMBER_SIZE:
                        raise _fail(f"{path.name}: member превышает лимит: {name}")
                    if name in payloads:
                        raise _fail(f"{path.name}: duplicate archive member: {name}")
                    total_size += member.size
                    if total_size > _MAX_UNCOMPRESSED_SIZE:
                        raise _fail(
                            f"{path.name}: uncompressed contents превышают лимит"
                        )
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise _fail(f"{path.name}: не удалось прочитать {name}")
                    payload = stream.read(member.size + 1)
                    if len(payload) != member.size:
                        raise _fail(f"{path.name}: размер member не совпадает: {name}")
                    payloads[name] = payload
    except (EOFError, tarfile.TarError, OSError) as error:
        raise _fail(f"Некорректный sdist {path}: {error}") from error
    return payloads


def _as_table(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _fail(f"pyproject.toml: {field} должен быть TOML table")
    return cast("dict[str, object]", value)


def _as_string_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(f"pyproject.toml: {field} должен быть списком строк")
    return tuple(cast("list[str]", value))


def _verify_sdist_pyproject(payload: bytes) -> None:
    try:
        document = cast("dict[str, object]", tomllib.loads(payload.decode("utf-8")))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _fail(f"Sdist содержит некорректный pyproject.toml: {error}") from error

    build_system = _as_table(document.get("build-system"), field="build-system")
    if "backend-path" in build_system:
        raise _fail("Sdist не должен подменять build backend через backend-path")
    backend = build_system.get("build-backend")
    if backend != "hatchling.build":
        raise _fail("Sdist должен использовать build-backend hatchling.build")
    build_requires = _as_string_list(
        build_system.get("requires"), field="build-system.requires"
    )
    build_dependency_names = {_requirement_name(item) for item in build_requires}
    if build_dependency_names != {"hatchling"}:
        raise _fail("Hatchling должен быть единственной build dependency")
    for requirement in build_requires:
        _canonical_requirement(requirement)

    project = _as_table(document.get("project"), field="project")
    if _normalize_name(str(project.get("name", ""))) != _DISTRIBUTION_NAME:
        raise _fail("pyproject.toml: project.name должен быть structuraguard")
    if project.get("version") != _VERSION:
        raise _fail(f"pyproject.toml: project.version должен быть {_VERSION}")
    if project.get("requires-python") != ">=3.12":
        raise _fail("pyproject.toml: project.requires-python должен быть >=3.12")

    tool_value = document.get("tool")
    if tool_value is not None:
        tool = _as_table(tool_value, field="tool")
        hatch_value = tool.get("hatch")
        if hatch_value is not None:
            hatch = _as_table(hatch_value, field="tool.hatch")
            build_value = hatch.get("build")
            if build_value is not None:
                hatch_build = _as_table(build_value, field="tool.hatch.build")
                if "hooks" in hatch_build:
                    raise _fail("Custom Hatch build hooks запрещены для M1 sdist")

    dependencies = _as_string_list(
        project.get("dependencies"), field="project.dependencies"
    )
    optional = _as_table(
        project.get("optional-dependencies"), field="project.optional-dependencies"
    )
    optional_names = {_normalize_name(name) for name in optional}
    if optional_names != _EXPECTED_EXTRAS:
        raise _fail(
            "pyproject.toml: набор optional-dependencies не совпадает с contract M1"
        )
    metadata_requirements = list(dependencies)
    for raw_extra, value in optional.items():
        extra = _normalize_name(raw_extra)
        for requirement in _as_string_list(
            value, field=f"project.optional-dependencies.{raw_extra}"
        ):
            metadata_requirements.append(f'{requirement}; extra == "{extra}"')
    _validate_dependency_contract(
        tuple(metadata_requirements),
        tuple(sorted(optional_names)),
        source="sdist pyproject.toml",
    )


def _verify_sdist(path: Path) -> SdistInspection:
    if path.name != _SDIST_NAME:
        raise _fail(f"Sdist должен называться {_SDIST_NAME}, получен {path.name}")
    payloads = _read_sdist(path)
    paths = set(payloads)
    allowed_gitignore = f"{_SDIST_ROOT}/.gitignore"
    _validate_no_forbidden_files(
        paths,
        source=path.name,
        allowed_gitignore=allowed_gitignore,
    )

    roots = {PurePosixPath(name).parts[0] for name in paths}
    if roots != {_SDIST_ROOT}:
        raise _fail(f"{path.name}: sdist должен иметь один root {_SDIST_ROOT}")
    relative_payloads = {
        PurePosixPath(name).relative_to(_SDIST_ROOT).as_posix(): payload
        for name, payload in payloads.items()
    }
    relative_paths = set(relative_payloads)
    required = {
        ".gitignore",
        "PKG-INFO",
        "pyproject.toml",
        *{f"src/{name}" for name in _PACKAGE_FILES},
    }
    if not required.issubset(relative_paths):
        missing = sorted(required - relative_paths)
        raise _fail(f"{path.name}: sdist не самодостаточен, отсутствуют {missing}")

    allowed_root_files = {
        "COPYING",
        ".gitignore",
        "LICENSE",
        "LICENSE.md",
        "PKG-INFO",
        "README.md",
        "README.rst",
        "pyproject.toml",
    }
    unexpected = {
        name
        for name in relative_paths
        if name not in allowed_root_files
        and name not in {f"src/{package_file}" for package_file in _PACKAGE_FILES}
    }
    if unexpected:
        raise _fail(f"{path.name}: неожиданные sdist contents: {sorted(unexpected)}")

    _verify_sdist_pyproject(relative_payloads["pyproject.toml"])
    metadata = _parse_metadata(relative_payloads["PKG-INFO"], source=path.name)
    package_payloads = {
        name: relative_payloads[f"src/{name}"] for name in _PACKAGE_FILES
    }
    return SdistInspection(
        metadata=metadata,
        package_payloads=package_payloads,
        pyproject_payload=relative_payloads["pyproject.toml"],
        gitignore_payload=relative_payloads[".gitignore"],
    )


def _verify_checkout_sources(
    *,
    inspection: SdistInspection,
    repository_root: Path,
) -> None:
    package_root = repository_root / "packages" / "structuraguard"
    pyproject_path = package_root / "pyproject.toml"
    if not pyproject_path.is_file():
        raise _fail(f"Package pyproject.toml не найден: {pyproject_path}")
    if pyproject_path.read_bytes() != inspection.pyproject_payload:
        raise _fail("Sdist pyproject.toml отличается от текущего reviewed checkout")
    gitignore_path = repository_root / ".gitignore"
    if not gitignore_path.is_file():
        raise _fail("Sdist содержит .gitignore, отсутствующий в checkout")
    if gitignore_path.read_bytes() != inspection.gitignore_payload:
        raise _fail("Sdist .gitignore отличается от текущего reviewed checkout")
    checkout_payloads: dict[str, bytes] = {}
    for package_file in _PACKAGE_FILES:
        source_path = package_root / "src" / package_file
        if not source_path.is_file():
            raise _fail(f"Package source не найден в checkout: {source_path}")
        checkout_payloads[package_file] = source_path.read_bytes()
    if inspection.package_payloads != checkout_payloads:
        raise _fail("Sdist package sources отличаются от текущего reviewed checkout")


def _offline_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_SUBPROCESS_ENV_KEYS
    }
    environment.update(
        {
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INDEX": "1",
            "PYTHONNOUSERSITE": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return environment


def _locked_registry_version(repository_root: Path, package_name: str) -> str:
    lock_path = repository_root / "uv.lock"
    try:
        document = cast(
            "dict[str, object]",
            tomllib.loads(lock_path.read_text(encoding="utf-8")),
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _fail(f"Не удалось прочитать lockfile {lock_path}: {error}") from error

    packages = document.get("package")
    if not isinstance(packages, list):
        raise _fail("uv.lock не содержит список package")
    versions: list[str] = []
    for raw_package in packages:
        if not isinstance(raw_package, dict):
            raise _fail("uv.lock содержит некорректную package entry")
        package = cast("dict[str, object]", raw_package)
        if _normalize_name(str(package.get("name", ""))) != _normalize_name(
            package_name
        ):
            continue
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            raise _fail(f"{package_name} в uv.lock должен происходить из registry")
        version = package.get("version")
        if not isinstance(version, str) or not version:
            raise _fail(f"{package_name} в uv.lock не имеет version")
        versions.append(version)
    if len(versions) != 1:
        raise _fail(
            f"uv.lock должен содержать одну версию {package_name}, получено {versions}"
        )
    return versions[0]


def _locked_registry_closure(
    repository_root: Path,
    root_package_name: str,
) -> dict[str, str]:
    lock_path = repository_root / "uv.lock"
    try:
        document = cast(
            "dict[str, object]",
            tomllib.loads(lock_path.read_text(encoding="utf-8")),
        )
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise _fail(f"Не удалось прочитать lockfile {lock_path}: {error}") from error

    raw_packages = document.get("package")
    if not isinstance(raw_packages, list):
        raise _fail("uv.lock не содержит список package")
    packages_by_name: dict[str, list[dict[str, object]]] = {}
    for raw_package in raw_packages:
        if not isinstance(raw_package, dict):
            raise _fail("uv.lock содержит некорректную package entry")
        package = cast("dict[str, object]", raw_package)
        normalized_name = _normalize_name(str(package.get("name", "")))
        packages_by_name.setdefault(normalized_name, []).append(package)

    pending = [_normalize_name(root_package_name)]
    closure: dict[str, str] = {}
    while pending:
        package_name = pending.pop()
        if package_name in closure:
            continue
        entries = packages_by_name.get(package_name, [])
        if len(entries) != 1:
            raise _fail(
                f"uv.lock должен содержать одну версию {package_name}, получено {len(entries)}"
            )
        package = entries[0]
        source = package.get("source")
        if not isinstance(source, dict) or "registry" not in source:
            raise _fail(f"{package_name} в uv.lock должен происходить из registry")
        version = package.get("version")
        if not isinstance(version, str) or not version:
            raise _fail(f"{package_name} в uv.lock не имеет version")
        closure[package_name] = version

        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise _fail(f"{package_name} в uv.lock имеет некорректные dependencies")
        for raw_dependency in dependencies:
            if not isinstance(raw_dependency, dict):
                raise _fail(
                    f"{package_name} в uv.lock имеет некорректную dependency entry"
                )
            dependency_name = raw_dependency.get("name")
            if not isinstance(dependency_name, str) or not dependency_name:
                raise _fail(
                    f"{package_name} в uv.lock имеет dependency без корректного name"
                )
            pending.append(_normalize_name(dependency_name))
    return dict(sorted(closure.items()))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    project_environment: Path | None = None,
) -> None:
    print(f"+ {shlex.join(command)}")
    environment = _offline_environment()
    if project_environment is not None:
        environment["UV_PROJECT_ENVIRONMENT"] = os.fspath(project_environment)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _fail(
            f"Команда превысила timeout {_COMMAND_TIMEOUT_SECONDS}s: "
            f"{shlex.join(command)}"
        ) from error
    except OSError as error:
        raise _fail(f"Не удалось запустить {command[0]}: {error}") from error
    if completed.returncode == 0:
        return
    output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    raise _fail(
        f"Команда завершилась с кодом {completed.returncode}: "
        f"{shlex.join(command)}\n{output[-6000:]}"
    )


def _find_uv() -> Path:
    executable = shutil.which("uv")
    if executable is None:
        raise _fail(
            "Для offline rebuild и установки runtime dependencies требуется uv из toolchain M1"
        )
    return Path(executable).resolve()


def _rebuild_wheel(
    *,
    uv: Path,
    sdist: Path,
    output_dir: Path,
    cwd: Path,
    locked_build_versions: dict[str, str],
) -> Path:
    output_dir.mkdir()
    build_constraints = output_dir.parent / "build-constraints.txt"
    build_constraints.write_text(
        "".join(
            f"{package_name}=={version}\n"
            for package_name, version in locked_build_versions.items()
        ),
        encoding="utf-8",
    )
    _run_checked(
        [
            os.fspath(uv),
            "build",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--force-pep517",
            "--build-constraints",
            os.fspath(build_constraints),
            "--wheel",
            "--clear",
            "--no-create-gitignore",
            "--out-dir",
            os.fspath(output_dir),
            os.fspath(sdist),
        ],
        cwd=cwd,
    )
    wheels = sorted(output_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise _fail(
            f"Rebuild из sdist должен создать ровно один wheel, получено {len(wheels)}"
        )
    return wheels[0]


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _probe_source(
    *,
    expected_version: str,
    expected_runtime_versions: dict[str, str],
    repository_root: Path,
) -> str:
    exports_json = json.dumps(sorted(_PUBLIC_EXPORTS))
    contract_exports_json = json.dumps(sorted(_CONTRACT_EXPORTS))
    domain_exports_json = json.dumps(sorted(_DOMAIN_EXPORTS))
    parser_exports_json = json.dumps(sorted(_PARSER_EXPORTS))
    builtin_parser_exports_json = json.dumps(sorted(_BUILTIN_PARSER_EXPORTS))
    port_exports_json = json.dumps(sorted(_PORT_EXPORTS))
    structure_exports_json = json.dumps(sorted(_STRUCTURE_EXPORTS))
    forbidden_json = json.dumps(sorted(_FORBIDDEN_DEPENDENCIES))
    optional_json = json.dumps(sorted(_FORBIDDEN_OPTIONAL_IMPORTS))
    runtime_versions_json = json.dumps(expected_runtime_versions, sort_keys=True)
    return textwrap.dedent(
        f"""
        import importlib.metadata as metadata
        import json
        import pathlib
        import re
        import sys

        expected_exports = set(json.loads({exports_json!r}))
        expected_contract_exports = set(json.loads({contract_exports_json!r}))
        expected_domain_exports = set(json.loads({domain_exports_json!r}))
        expected_parser_exports = set(json.loads({parser_exports_json!r}))
        expected_builtin_parser_exports = set(
            json.loads({builtin_parser_exports_json!r})
        )
        expected_port_exports = set(json.loads({port_exports_json!r}))
        expected_structure_exports = set(json.loads({structure_exports_json!r}))
        forbidden_roots = set(json.loads({forbidden_json!r}))
        optional_roots = set(json.loads({optional_json!r}))
        expected_runtime_versions = json.loads({runtime_versions_json!r})
        distribution = metadata.distribution("structuraguard")
        if distribution.version != {expected_version!r}:
            raise SystemExit(f"unexpected installed version: {{distribution.version}}")
        for package_name, expected_runtime_version in expected_runtime_versions.items():
            installed_runtime_version = metadata.version(package_name)
            if installed_runtime_version != expected_runtime_version:
                raise SystemExit(
                    f"unexpected installed {{package_name}} version: {{installed_runtime_version}}"
                )
        requirements = distribution.requires or []
        runtime = [
            item
            for item in requirements
            if re.search(r"\\bextra\\s*==", item, flags=re.IGNORECASE) is None
        ]
        runtime_names = []
        for requirement in runtime:
            match = re.match(r"^\\s*([A-Za-z0-9._-]+)", requirement)
            if match is None:
                raise SystemExit(f"invalid runtime dependency: {{requirement}}")
            runtime_names.append(re.sub(r"[-_.]+", "-", match.group(1)).lower())
        if len(runtime_names) != len(set(runtime_names)) or set(runtime_names) != {{
            "charset-normalizer",
            "pydantic",
        }}:
            raise SystemExit(f"unexpected runtime requirements: {{runtime}}")

        import structuraguard

        exports = tuple(structuraguard.__all__)
        if len(exports) != len(set(exports)) or set(exports) != expected_exports:
            raise SystemExit(f"unexpected __all__: {{exports}}")
        missing = sorted(name for name in expected_exports if not hasattr(structuraguard, name))
        if missing:
            raise SystemExit(f"missing top-level exports: {{missing}}")

        import structuraguard.contracts as contracts
        import structuraguard.domain as domain
        import structuraguard.parsers as parsers
        import structuraguard.parsers.builtin as builtin_parsers
        import structuraguard.parsers.tika as tika_parser
        import structuraguard.ports as ports
        import structuraguard.structure as structure

        if tika_parser.TikaParserAdapter().config.enabled:
            raise SystemExit("Tika must remain opt-in")
        if set(tika_parser.__all__) != {{"TikaConfig", "TikaEgressApproval", "TikaParserAdapter", "TikaParserLimits"}}:
            raise SystemExit("unexpected Tika exports")

        for module, expected in (
            (contracts, expected_contract_exports),
            (domain, expected_domain_exports),
            (parsers, expected_parser_exports),
            (builtin_parsers, expected_builtin_parser_exports),
            (ports, expected_port_exports),
            (structure, expected_structure_exports),
        ):
            module_exports = tuple(module.__all__)
            if len(module_exports) != len(set(module_exports)) or set(module_exports) != expected:
                raise SystemExit(
                    f"unexpected {{module.__name__}}.__all__: {{module_exports}}"
                )
            missing = sorted(name for name in expected if not hasattr(module, name))
            if missing:
                raise SystemExit(f"missing {{module.__name__}} exports: {{missing}}")
        module_path = pathlib.Path(structuraguard.__file__).resolve()
        environment_root = pathlib.Path(sys.prefix).resolve()
        repository_root = pathlib.Path({os.fspath(repository_root)!r}).resolve()
        if not module_path.is_relative_to(environment_root):
            raise SystemExit(f"package was not imported from the temporary venv: {{module_path}}")
        if module_path.is_relative_to(repository_root):
            raise SystemExit(f"package leaked from source checkout: {{module_path}}")
        direct_url = distribution.read_text("direct_url.json")
        if direct_url is not None and json.loads(direct_url).get("dir_info", {{}}).get("editable"):
            raise SystemExit("editable install detected")

        loaded_roots = {{name.partition(".")[0].lower() for name in sys.modules}}
        forbidden_loaded = sorted(loaded_roots & (forbidden_roots | optional_roots))
        if forbidden_loaded:
            raise SystemExit(f"forbidden dependency imported: {{forbidden_loaded}}")
        print(f"installed smoke OK: {{module_path}}")
        """
    ).strip()


def _verify_installed_wheel(
    *,
    uv: Path,
    wheel: Path,
    expected_runtime_versions: dict[str, str],
    temp_root: Path,
    repository_root: Path,
) -> None:
    venv_dir = temp_root / "venv"
    venv.EnvBuilder(with_pip=False, clear=True, symlinks=False).create(venv_dir)
    python = _venv_python(venv_dir)
    if not python.is_file():
        raise _fail(f"Не найден interpreter временного venv: {python}")

    _run_checked(
        [
            os.fspath(uv),
            "sync",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--locked",
            "--project",
            os.fspath(repository_root),
            "--package",
            _DISTRIBUTION_NAME,
            "--no-default-groups",
            "--no-install-workspace",
            "--python",
            os.fspath(python),
        ],
        cwd=temp_root,
        project_environment=venv_dir,
    )
    _run_checked(
        [
            os.fspath(uv),
            "pip",
            "install",
            "--offline",
            "--no-config",
            "--no-python-downloads",
            "--python",
            os.fspath(python),
            "--no-deps",
            os.fspath(wheel),
        ],
        cwd=temp_root,
    )
    import_probe = (
        repository_root
        / "packages"
        / "structuraguard"
        / "tests"
        / "smoke"
        / "import_probe.py"
    )
    if not import_probe.is_file():
        raise _fail(f"Strict import probe не найден: {import_probe}")
    _run_checked(
        [os.fspath(python), "-B", "-I", os.fspath(import_probe), "black-box"],
        cwd=temp_root,
    )
    _run_checked(
        [os.fspath(python), "-B", "-I", os.fspath(import_probe), "attribution"],
        cwd=temp_root,
    )
    _run_checked(
        [
            os.fspath(python),
            "-I",
            "-c",
            _probe_source(
                expected_version=_VERSION,
                expected_runtime_versions=expected_runtime_versions,
                repository_root=repository_root,
            ),
        ],
        cwd=temp_root,
    )


def _temporary_base(repository_root: Path) -> Path:
    candidate = Path(tempfile.gettempdir()).resolve()
    if not candidate.is_relative_to(repository_root):
        return candidate
    for fallback in (Path("/tmp"), Path("/private/tmp")):
        resolved = fallback.resolve()
        if resolved.is_dir() and not resolved.is_relative_to(repository_root):
            return resolved
    raise _fail("Не найден writable temporary directory вне repository")


def _select_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    if not dist_dir.is_dir():
        raise _fail(f"Каталог dist не найден: {dist_dir}")
    all_entries = sorted(dist_dir.iterdir())
    invalid_entries = [
        path.name for path in all_entries if not path.is_file() or path.is_symlink()
    ]
    if invalid_entries:
        raise _fail(f"Каталог dist содержит не regular files: {invalid_entries}")
    entries = all_entries
    wheels = [path for path in entries if path.suffix == ".whl"]
    sdists = [path for path in entries if path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise _fail(
            "Каталог dist должен содержать ровно один wheel и один .tar.gz sdist; "
            f"получено wheel={len(wheels)}, sdist={len(sdists)}"
        )
    unexpected = [path.name for path in entries if path not in {*wheels, *sdists}]
    if unexpected:
        raise _fail(f"Каталог dist содержит неожиданные файлы: {unexpected}")
    return wheels[0].resolve(), sdists[0].resolve()


def verify_distribution(dist_dir: Path) -> None:
    """Проверить wheel/sdist, offline rebuild и isolated import текущего SDK."""

    repository_root = Path(__file__).resolve().parents[1]
    wheel, sdist = _select_artifacts(dist_dir.resolve())
    original_wheel = _verify_wheel(wheel)
    inspected_sdist = _verify_sdist(sdist)
    _verify_checkout_sources(
        inspection=inspected_sdist,
        repository_root=repository_root,
    )
    if original_wheel.metadata != inspected_sdist.metadata:
        raise _fail("Core metadata wheel и sdist различается")
    if original_wheel.package_payloads != inspected_sdist.package_payloads:
        raise _fail("Package sources wheel и sdist различаются")

    uv = _find_uv()
    runtime_versions = {
        package_name: _locked_registry_version(repository_root, package_name)
        for package_name in sorted(_CORE_RUNTIME_CLOSURE)
    }
    build_versions = _locked_registry_closure(repository_root, "hatchling")
    temporary_base = _temporary_base(repository_root)
    with tempfile.TemporaryDirectory(
        prefix="structuraguard-distribution-",
        dir=temporary_base,
    ) as temporary_directory:
        temp_root = Path(temporary_directory).resolve()
        if temp_root.is_relative_to(repository_root):
            raise _fail(f"Temporary directory оказался внутри repository: {temp_root}")
        rebuilt_path = _rebuild_wheel(
            uv=uv,
            sdist=sdist,
            output_dir=temp_root / "rebuilt",
            cwd=temp_root,
            locked_build_versions=build_versions,
        )
        rebuilt_wheel = _verify_wheel(rebuilt_path)
        if rebuilt_wheel.metadata != original_wheel.metadata:
            raise _fail("Metadata rebuilt wheel отличается от исходного wheel")
        if rebuilt_wheel.package_payloads != original_wheel.package_payloads:
            raise _fail("Package contents rebuilt wheel отличаются от исходного wheel")
        if rebuilt_wheel.archive_files != original_wheel.archive_files:
            raise _fail("File manifest rebuilt wheel отличается от исходного wheel")
        if _file_sha256(rebuilt_path) != _file_sha256(wheel):
            raise _fail("Rebuilt wheel не совпадает с исходным wheel byte-for-byte")
        _verify_installed_wheel(
            uv=uv,
            wheel=wheel,
            expected_runtime_versions=runtime_versions,
            temp_root=temp_root,
            repository_root=repository_root,
        )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Проверить wheel/sdist StructuraGuard и offline rebuild/install smoke.",
    )
    parser.add_argument(
        "dist_dir", type=Path, help="Каталог с одним wheel и одним sdist"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    dist_dir = cast(Path, args.dist_dir)
    try:
        verify_distribution(dist_dir)
    except VerificationError as error:
        print(f"distribution verification FAILED: {error}", file=sys.stderr)
        return 1
    print("distribution verification OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
