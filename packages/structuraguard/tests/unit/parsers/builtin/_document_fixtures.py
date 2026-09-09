"""Малые детерминированные OOXML/PDF fixtures без зависимости от Office/OCR."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Literal

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "documents"


def package_parts(format_id: Literal["xlsx", "docx"]) -> dict[str, bytes]:
    main = "xl/workbook.xml" if format_id == "xlsx" else "word/document.xml"
    suffix = (
        "spreadsheetml.sheet" if format_id == "xlsx" else "wordprocessingml.document"
    )
    rels = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="'
        + main.encode()
        + b'"/></Relationships>'
    )
    parts = {
        "[Content_Types].xml": f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{suffix}.main+xml"/></Types>'.encode(),
        "_rels/.rels": rels,
    }
    if format_id == "xlsx":
        parts.update(
            {
                main: b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name=" Sheet " sheetId="1" r:id="r1"/><sheet name="hidden" sheetId="2" state="hidden" r:id="r2"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="r1" Type="worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="r2" Type="worksheet" Target="worksheets/sheet2.xml"/></Relationships>',
                "xl/worksheets/sheet1.xml": (FIXTURES / "sheet.xml").read_bytes(),
                "xl/worksheets/sheet2.xml": b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
                "xl/styles.xml": b'<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><cellXfs><xf numFmtId="14"/></cellXfs></styleSheet>',
            }
        )
    else:
        parts[main] = (FIXTURES / "document.xml").read_bytes()
    return parts


def zip_bytes(parts: dict[str, bytes], *, compressed: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer,
        "w",
        compression=zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED,
    ) as archive:
        for name, content in parts.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = archive.compression
            archive.writestr(info, content)
    return buffer.getvalue()


def pdf_bytes(
    *,
    text: str = "Physical text 001.20",
    action: str = "",
    pages: int = 1,
    grid: bool = False,
) -> bytes:
    """ASCII PDF с вычисляемым xref, text layer и необязательным inert action."""

    kids = " ".join(f"{5 + i * 2} 0 R" for i in range(pages))
    objects = [
        f"<< /Type /Catalog /Pages 2 0 R {action} >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Producer (fixture) >>",
    ]
    for _ in range(pages):
        content_ref = len(objects) + 2
        content = f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET" if text else "0 0 10 10 re S"
        if grid:
            content = (
                "0 0 0 RG 20 20 m 220 20 l 220 120 l 20 120 l h S 20 70 m 220 70 l S 120 20 m 120 120 l S "
                + " ".join(
                    f"BT /F1 12 Tf {x} {y} Td ({value}) Tj ET"
                    for x, y, value in [
                        (30, 90, "A"),
                        (130, 90, "B"),
                        (30, 40, "01"),
                        (130, 40, "02"),
                    ]
                )
            )
        objects.extend(
            [
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_ref} 0 R >>",
                f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            ]
        )
    data = bytearray(b"%PDF-1.7\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n{obj}\nendobj\n".encode("ascii"))
    start = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode())
    data.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info 4 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    )
    return bytes(data)
