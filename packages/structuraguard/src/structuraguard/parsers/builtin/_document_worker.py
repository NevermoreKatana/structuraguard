"""Одноразовый bounded process. Не является OS sandbox runner M12."""

from __future__ import annotations

import math
import os
import sys
import zipfile
from collections.abc import Iterator

from pydantic import TypeAdapter, ValidationError

from structuraguard.exceptions import ParserError, SecurityPolicyError

from ._common import limit_error
from ._documents import (
    DocumentBudget,
    DocumentRequest,
    DocumentUnit,
    SafePackage,
    worker_failure,
)
from ._markup import MarkupUnit, malformed
from .docx import DocxParserLimits, extract_docx
from .pdf import PdfParserLimits, extract_pdf
from .xlsx import XlsxParserLimits, extract_xlsx


def _units(request: DocumentRequest, path: str) -> Iterator[DocumentUnit]:
    import resource

    limits = (
        TypeAdapter(XlsxParserLimits).validate_python(request.limits)
        if request.format == "xlsx"
        else TypeAdapter(DocxParserLimits).validate_python(request.limits)
        if request.format == "docx"
        else TypeAdapter(PdfParserLimits).validate_python(request.limits)
    )
    memory = limits.worker_memory_mb * 1024 * 1024
    # Darwin не поддерживает уменьшение RLIMIT_AS: RSS контролирует parent watchdog.
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(
        resource.RLIMIT_CPU, (math.ceil(limits.timeout_seconds) + 1,) * 2
    )
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    budget = DocumentBudget(request, limits)
    if isinstance(limits, PdfParserLimits):
        yield from extract_pdf(path, budget, limits)
        return
    package = SafePackage(path, budget)
    try:
        if isinstance(limits, XlsxParserLimits):
            yield from extract_xlsx(package, budget, limits)
        else:
            yield from extract_docx(package, budget, limits)
    finally:
        package.close()


def main() -> None:
    """JSON transport исключает pickle и не принимает source paths от документа."""

    # Native/Python diagnostics не попадают в JSON transport.
    transport = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        request = DocumentRequest.model_validate_json(sys.stdin.buffer.readline(65537))
        for unit in _units(request, sys.argv[1]):
            measured = MarkupUnit((), unit.blocks, unit.tables)
            if (
                measured.size > request.limits["max_subtree_nodes"]
                or measured.chars > request.limits["max_subtree_chars"]
            ):
                raise limit_error(
                    adapter_id=f"builtin.{request.format}",
                    resource="document_unit",
                    limit=int(request.limits["max_subtree_nodes"]),
                )
            encoded = unit.model_dump_json()
            if len(encoded.encode("utf-8")) >= 16 * 1024 * 1024:
                raise limit_error(
                    adapter_id=f"builtin.{request.format}",
                    resource="worker_frame_bytes",
                    limit=16 * 1024 * 1024,
                )
            print(encoded, file=transport, flush=True)
    except (ParserError, SecurityPolicyError) as error:
        print(
            worker_failure(error).model_dump_json(),
            file=transport,
            flush=True,
        )
    except MemoryError:
        print(
            DocumentUnit(error_code="SECURITY_LIMIT_EXCEEDED").model_dump_json(),
            file=transport,
            flush=True,
        )
    except (
        RuntimeError,
        KeyError,
        IndexError,
        OverflowError,
    ):
        print(
            DocumentUnit(error_code="PARSER_OUTPUT_INVALID").model_dump_json(),
            file=transport,
            flush=True,
        )
    except (
        ValueError,
        OSError,
        zipfile.BadZipFile,
        ValidationError,
    ):
        # Не переносить native library snippets, metadata и URI в parent process.
        print(
            DocumentUnit(
                error_code=malformed("invalid_document").error_code
            ).model_dump_json(),
            file=transport,
            flush=True,
        )

    finally:
        transport.close()


if __name__ == "__main__":
    main()
