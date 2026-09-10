"""M05 над настоящим XLSX backend, без сети и имитации physical DTO."""

import io

import pytest
from openpyxl import Workbook
from tests.unit.structure.test_execution import execute, prepared

from structuraguard.contracts.source import SheetCellLocation
from structuraguard.parsers.builtin import XlsxParser

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_xlsx_pipeline_preserves_raw_lexemes_and_sheet_coordinates() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Данные"
    sheet.append(["name", "count"])
    sheet.append(["Ada", 1])
    sheet.append(["Bob", 2])
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    request, batches = await prepared(XlsxParser(), buffer.getvalue(), batch_size=1)
    output = await execute(request, batches)
    values = [
        v for b in output for r in b.records for e in r.entities for v in e.values
    ]
    assert [v.raw_value.value for v in values] == ["Ada", "1", "Bob", "2"]
    for value, coordinate in zip(values, ((1, 0), (1, 1), (2, 0), (2, 1)), strict=True):
        location = value.origins[0].location
        assert isinstance(location, SheetCellLocation)
        assert location.sheet_name == "Данные"
        assert (location.row_index, location.column_index) == coordinate
