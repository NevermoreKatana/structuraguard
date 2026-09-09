"""Unit и contract tests отдельного CSV/TSV adapter."""

from __future__ import annotations

import codecs
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from tests.contract_suites.parser import ParserContractCase, assert_parser_contract
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    metadata_map,
    source_for,
)

from structuraguard.contracts import (
    ExtractedBatch,
    ExtractedCell,
    ExtractedTable,
    PhysicalObjectKind,
    StringScalar,
    TabularCellLocation,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedParserLimits,
    DelimitedTextParser,
    builtin_delimited_parsers,
    builtin_text_parsers,
)


def _cells_by_coordinate(
    batches: tuple[ExtractedBatch, ...],
) -> dict[tuple[int, int], str]:
    cells: dict[tuple[int, int], str] = {}
    for batch in batches:
        for table in batch.tables:
            for cell in table.cells:
                assert isinstance(cell.value.location, TabularCellLocation)
                assert cell.value.location.row_index == cell.row_index
                assert cell.value.location.column_index == cell.column_index
                assert isinstance(cell.value.raw_value, StringScalar)
                cells[(cell.row_index, cell.column_index)] = cell.value.raw_value.value
    return cells


def _tables(batches: tuple[ExtractedBatch, ...]) -> tuple[ExtractedTable, ...]:
    return tuple(table for batch in batches for table in batch.tables)


def _cell_at(
    batches: tuple[ExtractedBatch, ...],
    row_index: int,
    column_index: int,
) -> ExtractedCell:
    return next(
        cell
        for table in _tables(batches)
        for cell in table.cells
        if (cell.row_index, cell.column_index) == (row_index, column_index)
    )


def _comma_override() -> DelimitedDetectionOptions:
    return DelimitedDetectionOptions(dialect_override=DelimitedDialect(delimiter=","))


@pytest.mark.anyio
async def test_delimited_contract_preserves_raw_cells_and_table_continuation() -> None:
    content = b'name,name,age\r\nAlice,"",42\r\nBob,"B, Jr.",37\r\n'
    source = source_for(
        content,
        display_name="people.csv",
        media_type="text/csv",
    )
    probe_context, parse_context = contexts_for(source, content, batch_size=2)
    parser = DelimitedTextParser()

    batches = await assert_parser_contract(
        ParserContractCase(
            parser=parser,
            source=source,
            probe_context=probe_context,
            parse_context=parse_context,
        )
    )

    tables = _tables(batches)
    assert len(tables) == 2
    assert tuple(table.table_id for table in tables) == (tables[0].table_id,) * 2
    assert tuple(table.segment_index for table in tables) == (0, 1)
    assert tuple((table.row_start_index, table.row_end_index) for table in tables) == (
        (0, 1),
        (2, 2),
    )
    assert tuple(table.is_last_segment for table in tables) == (False, True)
    for table in tables:
        assert isinstance(table.location, TabularCellLocation)
        assert table.location.table_id == table.table_id

    assert _cells_by_coordinate(batches) == {
        (0, 0): "name",
        (0, 1): "name",
        (0, 2): "age",
        (1, 0): "Alice",
        (1, 1): "",
        (1, 2): "42",
        (2, 0): "Bob",
        (2, 1): "B, Jr.",
        (2, 2): "37",
    }
    assert all(
        cell.value.technical_type_hint is None
        for table in tables
        for cell in table.cells
    )
    quoted_empty = _cell_at(batches, 1, 1)
    assert metadata_map(quoted_empty.metadata) == {
        "escaped": False,
        "explicit_empty": True,
        "quoted": True,
    }
    assert sum(batch.record_count or 0 for batch in batches) == 3
    assert batches[-1].manifest is not None
    assert batches[-1].manifest.record_count == 3
    assert (
        tuple(reference for batch in batches for reference in batch.indexed_refs)
        == batches[-1].manifest.source_index.refs
    )

    metadata = metadata_map(tables[0].metadata)
    assert metadata["delimiter"] == ","
    assert metadata["quote_char"] == '"'
    assert metadata["header_candidate_row"] == 0
    confidence = metadata["header_candidate_confidence"]
    assert isinstance(confidence, float)
    assert 0 < confidence <= 1


@pytest.mark.anyio
async def test_delimited_probe_detects_csv_tsv_and_quote_escape_dialect() -> None:
    cases = (
        (
            b'left,right\n"a,b",c\n"d,e",f\n',
            "spoof.tsv",
            "text/tab-separated-values",
            "csv",
            ",",
        ),
        (
            b'left\tright\n"a\tb"\tc\n"d\te"\tf\n',
            "spoof.csv",
            "text/csv",
            "tsv",
            "\t",
        ),
    )

    for content, display_name, media_type, format_id, delimiter in cases:
        source = source_for(
            content,
            display_name=display_name,
            media_type=media_type,
        )
        probe_context, parse_context = contexts_for(source, content)
        parser = DelimitedTextParser()

        probe = await parser.probe(source, probe_context)
        batches = await collect(
            parser,
            source,
            replace(parse_context, detected_encoding=probe.detected_encoding),
        )

        assert probe.supported
        assert probe.format_id == format_id
        assert probe.detected_encoding == "utf-8"
        tables = _tables(batches)
        assert metadata_map(tables[0].metadata)["delimiter"] == delimiter
        assert metadata_map(tables[0].metadata)["quote_char"] == '"'


@pytest.mark.anyio
async def test_default_detection_preserves_doubled_literal_escape_characters() -> None:
    content = b"path,name\nC:\\\\temp,A\nD:\\\\data,B\n"
    source = source_for(content, display_name="paths.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)

    default_parser = DelimitedTextParser()
    probe = await default_parser.probe(source, probe_context)
    default_batches = await collect(
        default_parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert metadata_map(_tables(default_batches)[0].metadata)["escape_char"] is None
    assert _cells_by_coordinate(default_batches)[(1, 0)] == "C:\\\\temp"
    assert _cells_by_coordinate(default_batches)[(2, 0)] == "D:\\\\data"

    escaped_parser = DelimitedTextParser(
        detection_options=DelimitedDetectionOptions(
            dialect_override=DelimitedDialect(
                delimiter=",",
                escape_char="\\",
            )
        )
    )
    escaped_batches = await collect(escaped_parser, source, parse_context)

    assert _cells_by_coordinate(escaped_batches)[(1, 0)] == "C:\\temp"
    assert _cells_by_coordinate(escaped_batches)[(2, 0)] == "D:\\data"


@pytest.mark.anyio
async def test_semicolon_detection_ignores_comma_rich_outlier() -> None:
    content = b"left;right\none;alpha\ntwo;beta\nthree;contains,many,commas\n"
    source = source_for(content, display_name="values.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser(limits=DelimitedParserLimits(max_columns=2))

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert probe.format_id == "csv"
    assert metadata_map(_tables(batches)[0].metadata)["delimiter"] == ";"
    assert _cells_by_coordinate(batches) == {
        (0, 0): "left",
        (0, 1): "right",
        (1, 0): "one",
        (1, 1): "alpha",
        (2, 0): "two",
        (2, 1): "beta",
        (3, 0): "three",
        (3, 1): "contains,many,commas",
    }


@pytest.mark.anyio
async def test_semicolon_detection_preserves_literal_comma_and_quotes() -> None:
    content = b'name;description\na;uses "x,y" literally\nb;other, thing\n'
    source = source_for(content, display_name="notes.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser(limits=DelimitedParserLimits(max_columns=2))

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert metadata_map(_tables(batches)[0].metadata)["delimiter"] == ";"
    assert _cells_by_coordinate(batches) == {
        (0, 0): "name",
        (0, 1): "description",
        (1, 0): "a",
        (1, 1): 'uses "x,y" literally',
        (2, 0): "b",
        (2, 1): "other, thing",
    }


@pytest.mark.anyio
async def test_delimited_detection_options_override_full_dialect() -> None:
    dialect = DelimitedDialect(
        delimiter="|",
        quote_char="'",
        escape_char="\\",
        double_quote=False,
    )
    options = DelimitedDetectionOptions(dialect_override=dialect)
    parser = DelimitedTextParser(detection_options=options)
    content = b"name|note\nAlice|'one\\'s note'\nBob|'two|parts'\n"
    source = source_for(
        content,
        display_name="custom.data",
        media_type="application/octet-stream",
    )
    probe_context, parse_context = contexts_for(source, content)

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert parser.detection_options == options
    assert probe.supported
    assert probe.format_id == "csv"
    assert _cells_by_coordinate(batches) == {
        (0, 0): "name",
        (0, 1): "note",
        (1, 0): "Alice",
        (1, 1): "one's note",
        (2, 0): "Bob",
        (2, 1): "two|parts",
    }
    metadata = metadata_map(_tables(batches)[0].metadata)
    assert metadata["delimiter"] == "|"
    assert metadata["quote_char"] == "'"
    assert metadata["escape_char"] == "\\"
    assert metadata["double_quote"] is False
    assert metadata_map(_cell_at(batches, 1, 1).metadata)["escaped"] is True


@pytest.mark.anyio
async def test_delimited_candidate_options_detect_custom_quote_and_escape() -> None:
    options = DelimitedDetectionOptions(
        delimiters=("|",),
        quote_chars=("'",),
        escape_chars=("\\",),
    )
    parser = DelimitedTextParser(detection_options=options)
    content = b"name|note\nAlice|'one\\'s note'\nBob|'two|parts'\n"
    source = source_for(
        content,
        display_name="custom.data",
        media_type="application/octet-stream",
    )
    probe_context, parse_context = contexts_for(source, content)

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert _cells_by_coordinate(batches)[(1, 1)] == "one's note"
    metadata = metadata_map(_tables(batches)[0].metadata)
    assert metadata["delimiter"] == "|"
    assert metadata["quote_char"] == "'"
    assert metadata["escape_char"] == "\\"


@pytest.mark.anyio
async def test_delimited_ragged_blank_and_present_empty_cells_are_distinct() -> None:
    content = b"a,b,c\n1,,3\n2\n,,\n\n"
    source = source_for(content, display_name="ragged.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content, batch_size=2)
    parser = DelimitedTextParser(detection_options=_comma_override())

    batches = await collect(parser, source, parse_context)

    assert _cells_by_coordinate(batches) == {
        (0, 0): "a",
        (0, 1): "b",
        (0, 2): "c",
        (1, 0): "1",
        (1, 1): "",
        (1, 2): "3",
        (2, 0): "2",
        (3, 0): "",
        (3, 1): "",
        (3, 2): "",
    }
    assert (2, 1) not in _cells_by_coordinate(batches)
    assert (4, 0) not in _cells_by_coordinate(batches)
    unquoted_empty = _cell_at(batches, 1, 1)
    assert metadata_map(unquoted_empty.metadata) == {
        "escaped": False,
        "explicit_empty": True,
        "quoted": False,
    }
    assert sum(batch.record_count or 0 for batch in batches) == 5
    tables = _tables(batches)
    assert tuple((table.row_start_index, table.row_end_index) for table in tables) == (
        (0, 1),
        (2, 3),
        (4, 4),
    )
    segment_metadata = tuple(metadata_map(table.metadata) for table in tables)
    assert tuple(metadata["row_count"] for metadata in segment_metadata) == (2, 2, 1)
    assert tuple(metadata["blank_row_count"] for metadata in segment_metadata) == (
        0,
        0,
        1,
    )
    assert tuple(metadata["minimum_column_count"] for metadata in segment_metadata) == (
        3,
        1,
        0,
    )
    assert tuple(metadata["maximum_column_count"] for metadata in segment_metadata) == (
        3,
        3,
        0,
    )
    assert tuple(metadata["ragged_rows"] for metadata in segment_metadata) == (
        False,
        True,
        False,
    )


@pytest.mark.anyio
async def test_delimited_flushes_complete_rows_at_cell_batch_limit() -> None:
    content = b"a,b\n1,2\n3,4\n5,6\n"
    source = source_for(content, display_name="cell-batches.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content, batch_size=100)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_columns=2, max_batch_cells=4),
        detection_options=_comma_override(),
    )

    batches = await collect(parser, source, parse_context)

    tables = _tables(batches)
    assert tuple(len(table.cells) for table in tables) == (4, 4)
    assert tuple(table.segment_index for table in tables) == (0, 1)
    assert tuple((table.row_start_index, table.row_end_index) for table in tables) == (
        (0, 1),
        (2, 3),
    )
    assert tuple(table.is_last_segment for table in tables) == (False, True)
    assert batches[-1].manifest is not None
    batches[-1].manifest.validate_batches(batches)


@pytest.mark.anyio
async def test_delimited_flushes_complete_rows_at_character_batch_limit() -> None:
    content = b"aa,b\ncc,d\nee,f\n"
    source = source_for(content, display_name="char-batches.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content, batch_size=100)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            max_columns=2,
            max_record_chars=4,
            max_batch_chars=10,
        ),
        detection_options=_comma_override(),
    )

    batches = await collect(parser, source, parse_context)

    tables = _tables(batches)
    assert tuple((table.row_start_index, table.row_end_index) for table in tables) == (
        (0, 1),
        (2, 2),
    )
    assert _cells_by_coordinate(batches) == {
        (0, 0): "aa",
        (0, 1): "b",
        (1, 0): "cc",
        (1, 1): "d",
        (2, 0): "ee",
        (2, 1): "f",
    }
    assert batches[-1].manifest is not None
    batches[-1].manifest.validate_batches(batches)


@pytest.mark.anyio
async def test_blank_rows_count_their_terminators_toward_character_batches() -> None:
    content = b"a,\n1,\n" + (b"\n" * 10)
    source = source_for(
        content, display_name="blank-batches.csv", media_type="text/csv"
    )
    _, parse_context = contexts_for(source, content, batch_size=100)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            max_columns=2,
            max_record_chars=2,
            max_batch_chars=4,
        ),
        detection_options=_comma_override(),
    )

    batches = await collect(parser, source, parse_context)

    assert tuple(batch.record_count for batch in batches) == (1, 2, 4, 4, 1)
    assert tuple(
        (table.row_start_index, table.row_end_index) for table in _tables(batches)
    ) == ((0, 0), (1, 2), (3, 6), (7, 10), (11, 11))
    assert batches[-1].manifest is not None
    batches[-1].manifest.validate_batches(batches)


@pytest.mark.anyio
async def test_adaptive_batching_respects_max_batches() -> None:
    content = b"a,b\n1,2\n3,4\n"
    source = source_for(content, display_name="batch-limit.csv", media_type="text/csv")
    _, parse_context = contexts_for(
        source,
        content,
        batch_size=100,
        max_batches=2,
    )
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_columns=2, max_batch_cells=2),
        detection_options=_comma_override(),
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": "builtin.delimited",
        "limit": 2,
        "resource": "batch_count",
    }


@pytest.mark.anyio
async def test_batch_resource_limits_change_parser_options_fingerprint() -> None:
    content = b"aa,b\ncc,d\n"
    source = source_for(content, display_name="fingerprint.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content, batch_size=100)
    limits = (
        DelimitedParserLimits(
            max_columns=2,
            max_record_chars=4,
            max_batch_cells=4,
            max_batch_chars=8,
        ),
        DelimitedParserLimits(
            max_columns=2,
            max_record_chars=4,
            max_batch_cells=6,
            max_batch_chars=8,
        ),
        DelimitedParserLimits(
            max_columns=2,
            max_record_chars=4,
            max_batch_cells=4,
            max_batch_chars=12,
        ),
    )

    fingerprints: set[str] = set()
    for parser_limits in limits:
        batches = await collect(
            DelimitedTextParser(
                limits=parser_limits,
                detection_options=_comma_override(),
            ),
            source,
            parse_context,
        )
        assert batches[-1].manifest is not None
        fingerprint = batches[-1].manifest.parser_options_fingerprint
        assert fingerprint is not None
        fingerprints.add(fingerprint)

    assert len(fingerprints) == 3


@pytest.mark.anyio
async def test_delimited_header_is_only_bounded_candidate_metadata() -> None:
    content = b"customer,amount\nAlice,10.50\nBob,11.25\n"
    source = source_for(content, display_name="orders.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)

    batches = await collect(DelimitedTextParser(), source, parse_context)

    cells = _cells_by_coordinate(batches)
    assert cells[(0, 0)] == "customer"
    assert cells[(0, 1)] == "amount"
    assert (0, 0) in cells
    metadata = metadata_map(_tables(batches)[0].metadata)
    assert metadata["header_candidate_row"] == 0
    assert all(
        cell.cell_id.startswith("cell-")
        and "customer" not in cell.cell_id
        and "amount" not in cell.cell_id
        for table in _tables(batches)
        for cell in table.cells
    )


@pytest.mark.anyio
async def test_delimited_does_not_normalize_locale_or_business_values() -> None:
    content = (
        b'amount,date,status\n"1.234,50",02/09/2026,pending\n0012,2026-09-02,TRUE\n'
    )
    source = source_for(content, display_name="raw.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)

    batches = await collect(DelimitedTextParser(), source, parse_context)

    cells = _cells_by_coordinate(batches)
    assert cells[(1, 0)] == "1.234,50"
    assert cells[(1, 1)] == "02/09/2026"
    assert cells[(2, 0)] == "0012"
    assert cells[(2, 2)] == "TRUE"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content",
    (
        b"only one physical column\nsecond line\n",
        b"a,b\nc;d\ne|f\n",
        b"Hello, this is prose, with commas\nAnother sentence, has a comma\n",
        b"First, line\nSecond, has, different, commas\n",
    ),
    ids=(
        "single-column",
        "unstable-delimiter",
        "uneven-comma-prose",
        "ragged-comma-prose",
    ),
)
async def test_delimited_probe_declines_weak_or_unstable_structure(
    content: bytes,
) -> None:
    source = source_for(content, display_name="ambiguous.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)

    probe = await DelimitedTextParser().probe(source, probe_context)

    assert not probe.supported
    assert probe.format_id is None
    assert probe.confidence == Decimal("0")


@pytest.mark.anyio
async def test_ragged_two_row_auto_detection_declines_but_override_enforces_limit() -> (
    None
):
    content = b"a,b\n1,2,3\n"
    source = source_for(content, display_name="ragged.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)

    probe = await DelimitedTextParser().probe(source, probe_context)

    assert not probe.supported
    assert probe.format_id is None

    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), DelimitedTextParser()))
    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "builtin.text"
    assert selected.probe_result.format_id == "txt"

    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_columns=2),
        detection_options=_comma_override(),
    )
    with pytest.raises(SecurityPolicyError) as raised:
        await parser.probe(source, probe_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": "builtin.delimited",
        "limit": 2,
        "resource": "columns",
    }


@pytest.mark.anyio
async def test_delimited_probe_fails_closed_for_equally_plausible_dialects() -> None:
    content = b"a,b;c\n1,2;3\n"
    source = source_for(content, display_name="ambiguous.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(ParserError) as raised:
        await DelimitedTextParser().probe(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "expected_line"),
    (
        (b'a,b\n"unterminated,field\n', 2),
        (b'a,b\n"closed"tail,value\n', 2),
    ),
    ids=("unterminated-quote", "characters-after-quote"),
)
async def test_delimited_malformed_row_has_typed_physical_line_number(
    content: bytes,
    expected_line: int,
) -> None:
    source = source_for(content, display_name="broken.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser(detection_options=_comma_override())

    with pytest.raises(ParserError) as raised:
        await collect(parser, source, parse_context)

    assert raised.value.error_code == "PARSER_MALFORMED_INPUT"
    assert raised.value.details["line_number"] == expected_line
    assert raised.value.details["record_number"] == 2
    assert raised.value.details["reason"] in {
        "unterminated_quote",
        "unexpected_character_after_quote",
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("limits", "content", "resource", "limit"),
    (
        (
            DelimitedParserLimits(),
            b"a,b\n1,2\n3,4\n",
            "records",
            2,
        ),
        (
            DelimitedParserLimits(max_columns=2),
            b"a,b\n1,2,3\n",
            "columns",
            2,
        ),
        (
            DelimitedParserLimits(max_field_size=3),
            b"a,b\n1234,2\n",
            "field_size",
            3,
        ),
        (
            DelimitedParserLimits(max_record_chars=5),
            b"a,b\n123,45\n",
            "record_chars",
            5,
        ),
    ),
    ids=("records", "columns", "field-size", "record-chars"),
)
async def test_delimited_enforces_format_limits_with_typed_resource(
    limits: DelimitedParserLimits,
    content: bytes,
    resource: str,
    limit: int,
) -> None:
    source = source_for(content, display_name="limited.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)
    if resource == "records":
        parse_context = replace(parse_context, max_records=limit)
    parser = DelimitedTextParser(
        limits=limits,
        detection_options=_comma_override(),
    )

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)

    assert raised.value.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert raised.value.details == {
        "adapter_id": "builtin.delimited",
        "limit": limit,
        "resource": resource,
    }


@pytest.mark.anyio
async def test_delimited_allows_exact_record_column_field_and_record_boundaries() -> (
    None
):
    content = b"a,b\n12,3\n"
    source = source_for(content, display_name="boundary.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)
    parse_context = replace(parse_context, max_records=2)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(
            max_columns=2,
            max_field_size=2,
            max_record_chars=4,
        ),
        detection_options=_comma_override(),
    )

    batches = await collect(parser, source, parse_context)

    assert _cells_by_coordinate(batches)[(1, 0)] == "12"
    assert sum(batch.record_count or 0 for batch in batches) == 2


@pytest.mark.anyio
async def test_delimited_blank_physical_rows_count_toward_max_records() -> None:
    content = b"a,b\n\n"
    source = source_for(content, display_name="blank.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)
    parse_context = replace(parse_context, max_records=1)
    parser = DelimitedTextParser(detection_options=_comma_override())

    with pytest.raises(SecurityPolicyError) as raised:
        await collect(parser, source, parse_context)

    assert raised.value.details["resource"] == "records"
    assert raised.value.details["limit"] == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "display_name", "media_type", "expected_adapter", "format_id"),
    (
        (b"a,b\n1,2\n", "spoof.txt", "text/plain", "builtin.delimited", "csv"),
        (
            b"a\tb\n1\t2\n",
            "spoof.csv",
            "text/csv",
            "builtin.delimited",
            "tsv",
        ),
        (
            b"only one column\nsecond line\n",
            "claimed.csv",
            "text/csv",
            "builtin.text",
            "txt",
        ),
        (
            b"Hello, world\nThis ordinary prose has no delimiter\n",
            "prose.csv",
            "text/csv",
            "builtin.text",
            "txt",
        ),
        (
            b"Hello, this is prose, with commas\nAnother sentence, has a comma\n",
            "uneven-prose.csv",
            "text/csv",
            "builtin.text",
            "txt",
        ),
        (b"", "empty.csv", "text/csv", "builtin.text", "txt"),
    ),
    ids=(
        "csv",
        "tsv",
        "single-column",
        "comma-prose",
        "uneven-comma-prose",
        "empty",
    ),
)
async def test_all_builtin_selection_respects_delimited_content_ownership(
    content: bytes,
    display_name: str,
    media_type: str,
    expected_adapter: str,
    format_id: str,
) -> None:
    source = source_for(content, display_name=display_name, media_type=media_type)
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), *builtin_delimited_parsers()))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == expected_adapter
    assert selected.probe_result.format_id == format_id


@pytest.mark.anyio
async def test_all_builtin_selection_does_not_hide_ambiguous_dialect() -> None:
    content = b"a,b;c\n1,2;3\n"
    source = source_for(content, display_name="ambiguous.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), *builtin_delimited_parsers()))

    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_all_builtin_selection_rejects_binary_spoof() -> None:
    content = b"a,b\n1,\x00\n"
    source = source_for(content, display_name="binary.csv", media_type="text/csv")
    probe_context, _ = contexts_for(source, content)
    registry = ParserRegistry()
    registry.register_many((*builtin_text_parsers(), *builtin_delimited_parsers()))

    with pytest.raises(ParserError) as raised:
        async with registry.session() as session:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FORMAT"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("content", "expected_encoding", "expected_first_cell"),
    (
        (
            codecs.BOM_UTF8 + "имя,город\nАлиса,Москва\nБоб,Казань\n".encode(),
            "utf-8-sig",
            "имя",
        ),
        (
            (
                "имя,описание\n"
                "Алиса,Подробное описание записи на русском языке\n"
                "Борис,Ещё одно подробное описание для определения кодировки\n"
                "Вера,Третья строка с достаточно связным русским текстом\n"
            ).encode("cp1251"),
            "cp1251",
            "имя",
        ),
    ),
    ids=("utf8-bom", "legacy-cp1251"),
)
async def test_delimited_shared_encoding_detection_preserves_first_cell(
    content: bytes,
    expected_encoding: str,
    expected_first_cell: str,
) -> None:
    source = source_for(content, display_name="encoded.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert probe.detected_encoding == expected_encoding
    assert _cells_by_coordinate(batches)[(0, 0)] == expected_first_cell


@pytest.mark.anyio
async def test_delimited_quoted_newline_survives_one_byte_chunks_and_batches() -> None:
    content = b'name,note\r\nAlice,"line one\r\nline two"\r\nBob,end\r\n'
    source = source_for(content, display_name="multiline.csv", media_type="text/csv")
    reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=1,
    )
    _, parse_context = contexts_for(
        source,
        content,
        batch_size=1,
        reader=reader,
        detected_encoding="utf-8",
    )
    parser = DelimitedTextParser(detection_options=_comma_override())

    batches = await collect(parser, source, parse_context)

    assert _cells_by_coordinate(batches)[(1, 1)] == "line one\r\nline two"
    assert metadata_map(_cell_at(batches, 1, 1).metadata)["quoted"] is True
    assert tuple(
        (table.row_start_index, table.row_end_index) for table in _tables(batches)
    ) == ((0, 0), (1, 1), (2, 2))


@pytest.mark.anyio
async def test_manifest_validates_table_continuity_and_rejects_segment_tamper() -> None:
    content = b"a,b\n1,2\n3,4\n"
    source = source_for(content, display_name="segments.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content, batch_size=1)
    batches = await collect(
        DelimitedTextParser(detection_options=_comma_override()),
        source,
        parse_context,
    )
    manifest = batches[-1].manifest
    assert manifest is not None

    manifest.validate_batches(batches)
    second_table = batches[1].tables[0]
    tampered_table = second_table.model_copy(
        update={"segment_index": 7, "row_start_index": 7, "row_end_index": 7}
    )
    tampered_batch = batches[1].model_copy(update={"tables": (tampered_table,)})

    with pytest.raises(ValueError, match="непрерывную sequence"):
        manifest.validate_batches((batches[0], tampered_batch, *batches[2:]))


@pytest.mark.anyio
async def test_delimited_source_index_contains_exact_table_cell_value_prefix() -> None:
    content = b"a,b\n1,2\n"
    source = source_for(content, display_name="index.csv", media_type="text/csv")
    _, parse_context = contexts_for(source, content)

    batches = await collect(
        DelimitedTextParser(detection_options=_comma_override()),
        source,
        parse_context,
    )

    batch = batches[0]
    assert tuple((ref.kind, ref.local_id) for ref in batch.indexed_refs) == (
        (PhysicalObjectKind.TABLE, "table-0"),
        (PhysicalObjectKind.CELL, "cell-0"),
        (PhysicalObjectKind.CELL, "cell-1"),
        (PhysicalObjectKind.CELL, "cell-2"),
        (PhysicalObjectKind.CELL, "cell-3"),
        (PhysicalObjectKind.VALUE, "value-0"),
        (PhysicalObjectKind.VALUE, "value-1"),
        (PhysicalObjectKind.VALUE, "value-2"),
        (PhysicalObjectKind.VALUE, "value-3"),
    )
    assert batch.manifest is not None
    assert batch.indexed_refs == batch.manifest.source_index.refs


@pytest.mark.anyio
async def test_delimited_autodetect_keeps_literal_backslashes_raw() -> None:
    content = b'path,name\n"C:\\temp",A\n"D:\\data",B\n"E:\\0",C\n'
    source = source_for(content, display_name="paths.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert metadata_map(_tables(batches)[0].metadata)["escape_char"] is None
    assert _cells_by_coordinate(batches)[(1, 0)] == "C:\\temp"
    assert _cells_by_coordinate(batches)[(2, 0)] == "D:\\data"
    assert _cells_by_coordinate(batches)[(3, 0)] == "E:\\0"
    assert metadata_map(_cell_at(batches, 1, 0).metadata) == {
        "escaped": False,
        "explicit_empty": False,
        "quoted": True,
    }


@pytest.mark.anyio
async def test_delimited_autodetect_recognizes_escaped_delimiters() -> None:
    content = b"left,right\none\\,two,x\nthree\\,four,y\n"
    source = source_for(content, display_name="escaped.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert probe.format_id == "csv"
    assert metadata_map(_tables(batches)[0].metadata)["delimiter"] == ","
    assert metadata_map(_tables(batches)[0].metadata)["escape_char"] == "\\"
    assert _cells_by_coordinate(batches) == {
        (0, 0): "left",
        (0, 1): "right",
        (1, 0): "one,two",
        (1, 1): "x",
        (2, 0): "three,four",
        (2, 1): "y",
    }
    assert metadata_map(_cell_at(batches, 1, 0).metadata)["escaped"] is True
    assert metadata_map(_cell_at(batches, 2, 0).metadata)["escaped"] is True


@pytest.mark.anyio
async def test_headerless_escape_tie_fails_closed_without_changing_raw_projection() -> (
    None
):
    content = b"one\\,two,x\nthree\\,four,y\n"
    source = source_for(
        content,
        display_name="escape-tie.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(ParserError) as raised:
        await DelimitedTextParser().probe(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_bounded_probe_prefers_non_destructive_escape_for_unseen_tail() -> None:
    content = b"left,right\none,two\nC:\\temp,x\n"
    source = source_for(
        content,
        display_name="bounded-escape.csv",
        media_type="text/csv",
    )
    options = DelimitedDetectionOptions(
        delimiters=(",",),
        quote_chars=('"',),
        escape_chars=("\\", None),
    )
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_header_probe_rows=2),
        detection_options=options,
    )
    probe_context, parse_context = contexts_for(source, content)

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert metadata_map(_tables(batches)[0].metadata)["escape_char"] is None
    assert _cells_by_coordinate(batches)[(2, 0)] == "C:\\temp"
    assert metadata_map(_cell_at(batches, 2, 0).metadata)["escaped"] is False


@pytest.mark.anyio
async def test_equally_evidenced_quote_characters_fail_closed() -> None:
    content = b"\"a\",\"b\"\n'c','d'\n"
    source = source_for(
        content,
        display_name="quote-ambiguity.csv",
        media_type="text/csv",
    )
    probe_context, _ = contexts_for(source, content)

    with pytest.raises(ParserError) as raised:
        await DelimitedTextParser().probe(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FEATURE"
    assert raised.value.details == {
        "feature": "ambiguous_dialect",
        "reason": "insufficient_structure",
    }


@pytest.mark.anyio
async def test_real_double_quote_evidence_wins_over_malformed_apostrophe_candidate() -> (
    None
):
    content = b"a,b\n\"x,y\",z\n'foo',bar\n'baz'tail,qux\n"
    source = source_for(
        content,
        display_name="mixed-quotes.csv",
        media_type="text/csv",
    )
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert probe.format_id == "csv"
    assert metadata_map(_tables(batches)[0].metadata)["quote_char"] == '"'
    assert _cells_by_coordinate(batches) == {
        (0, 0): "a",
        (0, 1): "b",
        (1, 0): "x,y",
        (1, 1): "z",
        (2, 0): "'foo'",
        (2, 1): "bar",
        (3, 0): "'baz'tail",
        (3, 1): "qux",
    }
    assert metadata_map(_cell_at(batches, 1, 0).metadata)["quoted"] is True
    assert metadata_map(_cell_at(batches, 2, 0).metadata)["quoted"] is False


@pytest.mark.anyio
async def test_utf16le_crlf_one_byte_reads_do_not_create_blank_rows() -> None:
    content = codecs.BOM_UTF16_LE + "a,b\r\n1,2\r\n".encode("utf-16-le")
    source = source_for(
        content,
        display_name="utf16.csv",
        media_type="text/csv",
    )
    reader = ShortReadSourceReader(
        source.source_fingerprint,
        content,
        max_chunk_bytes=1,
    )
    probe_context, parse_context = contexts_for(
        source,
        content,
        batch_size=1,
        reader=reader,
    )
    parser = DelimitedTextParser(detection_options=_comma_override())

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert probe.detected_encoding == "utf-16-le"
    assert _cells_by_coordinate(batches) == {
        (0, 0): "a",
        (0, 1): "b",
        (1, 0): "1",
        (1, 1): "2",
    }
    assert sum(batch.record_count or 0 for batch in batches) == 2
    assert tuple(
        (table.row_start_index, table.row_end_index) for table in _tables(batches)
    ) == ((0, 0), (1, 1))
    assert all(cell.row_index < 2 for table in _tables(batches) for cell in table.cells)


@pytest.mark.anyio
async def test_exact_dialect_override_allows_single_header_probe_row() -> None:
    content = b"a,b\n"
    source = source_for(content, display_name="forced.csv", media_type="text/csv")
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser(
        limits=DelimitedParserLimits(max_header_probe_rows=1),
        detection_options=_comma_override(),
    )

    probe = await parser.probe(source, probe_context)
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )

    assert probe.supported
    assert _cells_by_coordinate(batches) == {(0, 0): "a", (0, 1): "b"}


@pytest.mark.parametrize(
    "factory",
    (
        lambda: DelimitedDialect(delimiter=",", quote_char=","),
        lambda: DelimitedDialect(delimiter="\n"),
        lambda: DelimitedDetectionOptions(delimiters=(",", ",")),
        lambda: DelimitedParserLimits(max_columns=10_001),
        lambda: DelimitedParserLimits(max_field_size=16 * 1024 * 1024 + 1),
        lambda: DelimitedParserLimits(max_record_chars=32 * 1024 * 1024 + 1),
        lambda: DelimitedParserLimits(max_batch_cells=0),
        lambda: DelimitedParserLimits(max_batch_cells=10_001),
        lambda: DelimitedParserLimits(max_batch_chars=32 * 1024 * 1024 + 3),
        lambda: DelimitedParserLimits(max_columns=3, max_batch_cells=2),
        lambda: DelimitedParserLimits(max_record_chars=9, max_batch_chars=8),
        lambda: DelimitedParserLimits(max_header_probe_rows=1_025),
        lambda: DelimitedParserLimits(max_dialect_candidates=129),
        lambda: DelimitedTextParser(
            limits=DelimitedParserLimits(max_header_probe_rows=1)
        ),
        lambda: DelimitedTextParser(
            detection_options=DelimitedDetectionOptions(
                delimiters=(",",),
                quote_chars=(",",),
                escape_chars=(None,),
            )
        ),
        lambda: DelimitedDetectionOptions(
            delimiters=tuple(chr(0x100 + index) for index in range(129))
        ),
        lambda: DelimitedTextParser(
            limits=DelimitedParserLimits(max_dialect_candidates=1),
            detection_options=DelimitedDetectionOptions(
                delimiters=(",",),
                quote_chars=('"',),
                escape_chars=(None, "\\"),
            ),
        ),
        lambda: DelimitedTextParser(limits=False),  # type: ignore[arg-type]
        lambda: DelimitedTextParser(
            detection_options=False,  # type: ignore[arg-type]
        ),
    ),
    ids=(
        "overlapping-syntax",
        "newline-delimiter",
        "duplicate-candidate",
        "columns-hard-cap",
        "field-hard-cap",
        "record-hard-cap",
        "zero-batch-cells",
        "batch-cells-hard-cap",
        "batch-chars-hard-cap",
        "batch-cells-smaller-than-row",
        "batch-chars-smaller-than-record",
        "header-probe-hard-cap",
        "candidate-hard-cap",
        "auto-single-probe-row",
        "empty-valid-cartesian-product",
        "candidate-vocabulary-hard-cap",
        "candidate-product",
        "false-limits",
        "false-detection-options",
    ),
)
def test_delimited_invalid_options_and_hard_caps_fail_closed(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()
