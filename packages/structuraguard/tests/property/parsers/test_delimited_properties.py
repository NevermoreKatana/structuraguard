"""Property-based invariants для lossless CSV/TSV parsing."""

from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass, replace

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.parsers.builtin._support import (
    ShortReadSourceReader,
    collect,
    contexts_for,
    source_for,
)

from structuraguard.contracts import ExtractedBatch, StringScalar
from structuraguard.parsers.builtin import (
    DelimitedDetectionOptions,
    DelimitedDialect,
    DelimitedTextParser,
)

_DELIMITERS = (",", "\t", ";", "|")
_UNICODE_TEXT = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cc", "Cs"),
        blacklist_characters=("\x00", "\r"),
    ),
    max_size=12,
)


@dataclass(frozen=True, slots=True)
class _GeneratedTable:
    delimiter: str
    rows: tuple[tuple[str, ...], ...]
    escape_char: str | None
    double_quote: bool


@st.composite
def _generated_tables(
    draw: st.DrawFn,
    *,
    variable_escape: bool,
) -> _GeneratedTable:
    delimiter = draw(st.sampled_from(_DELIMITERS))
    column_count = draw(st.integers(min_value=2, max_value=4))
    row_count = draw(st.integers(min_value=2, max_value=6))
    special_values = (
        "",
        f"before{delimiter}after",
        'quote " inside',
        "две\nстроки",
        "東京🙂",
        "Zażółć gęślą jaźń",
    )
    cell_strategy = st.one_of(_UNICODE_TEXT, st.sampled_from(special_values))
    rows = tuple(
        tuple(
            draw(st.lists(cell_strategy, min_size=column_count, max_size=column_count))
        )
        for _ in range(row_count)
    )
    escaped = draw(st.booleans()) if variable_escape else False
    return _GeneratedTable(
        delimiter=delimiter,
        rows=rows,
        escape_char="\\" if escaped else None,
        double_quote=not escaped,
    )


def _encode_table(case: _GeneratedTable) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(
        output,
        delimiter=case.delimiter,
        quotechar='"',
        escapechar=case.escape_char,
        doublequote=case.double_quote,
        quoting=csv.QUOTE_ALL,
        lineterminator="\n",
    )
    writer.writerows(case.rows)
    return output.getvalue().encode()


def _row_projection(
    batches: tuple[ExtractedBatch, ...],
) -> tuple[tuple[str, ...], ...]:
    by_row: dict[int, dict[int, str]] = {}
    for batch in batches:
        for table in batch.tables:
            for cell in table.cells:
                assert isinstance(cell.value.raw_value, StringScalar)
                by_row.setdefault(cell.row_index, {})[cell.column_index] = (
                    cell.value.raw_value.value
                )
    return tuple(
        tuple(columns[index] for index in range(len(columns)))
        for _, columns in sorted(by_row.items())
    )


async def _probe_and_parse(
    case: _GeneratedTable,
) -> tuple[str, tuple[ExtractedBatch, ...]]:
    content = _encode_table(case)
    suffix = "tsv" if case.delimiter == "\t" else "csv"
    source = source_for(
        content,
        display_name=f"generated.{suffix}",
        media_type=("text/tab-separated-values" if suffix == "tsv" else "text/csv"),
    )
    probe_context, parse_context = contexts_for(source, content)
    parser = DelimitedTextParser()
    probe = await parser.probe(source, probe_context)
    assert probe.supported
    batches = await collect(
        parser,
        source,
        replace(parse_context, detected_encoding=probe.detected_encoding),
    )
    return probe.format_id or "", batches


@settings(max_examples=40, deadline=None)
@given(case=_generated_tables(variable_escape=False))
def test_detected_delimiter_and_quoting_round_trip_unicode_tables(
    case: _GeneratedTable,
) -> None:
    format_id, batches = asyncio.run(_probe_and_parse(case))

    assert format_id == ("tsv" if case.delimiter == "\t" else "csv")
    assert _row_projection(batches) == case.rows


async def _parse_with_boundaries(
    case: _GeneratedTable,
    *,
    batch_size: int,
    max_chunk_bytes: int | None,
) -> tuple[ExtractedBatch, ...]:
    content = _encode_table(case)
    source = source_for(
        content,
        display_name="configured.data",
        media_type="application/octet-stream",
    )
    reader = (
        ShortReadSourceReader(
            source.source_fingerprint,
            content,
            max_chunk_bytes=max_chunk_bytes,
        )
        if max_chunk_bytes is not None
        else None
    )
    _, parse_context = contexts_for(
        source,
        content,
        batch_size=batch_size,
        reader=reader,
        detected_encoding="utf-8",
    )
    dialect = DelimitedDialect(
        delimiter=case.delimiter,
        quote_char='"',
        escape_char=case.escape_char,
        double_quote=case.double_quote,
    )
    parser = DelimitedTextParser(
        detection_options=DelimitedDetectionOptions(dialect_override=dialect)
    )
    return await collect(parser, source, parse_context)


@settings(max_examples=50, deadline=None)
@given(
    case=_generated_tables(variable_escape=True),
    batch_size=st.integers(min_value=1, max_value=5),
    max_chunk_bytes=st.integers(min_value=1, max_value=11),
)
def test_raw_projection_is_invariant_to_chunk_and_batch_boundaries(
    case: _GeneratedTable,
    batch_size: int,
    max_chunk_bytes: int,
) -> None:
    fragmented = asyncio.run(
        _parse_with_boundaries(
            case,
            batch_size=batch_size,
            max_chunk_bytes=max_chunk_bytes,
        )
    )
    baseline = asyncio.run(
        _parse_with_boundaries(
            case,
            batch_size=100,
            max_chunk_bytes=None,
        )
    )
    same_batch_unfragmented = asyncio.run(
        _parse_with_boundaries(
            case,
            batch_size=batch_size,
            max_chunk_bytes=None,
        )
    )

    assert _row_projection(fragmented) == case.rows
    assert fragmented == same_batch_unfragmented
    assert _row_projection(fragmented) == _row_projection(baseline)
    assert tuple(
        table.row_start_index for batch in fragmented for table in batch.tables
    ) == tuple(range(0, len(case.rows), batch_size))
    assert all(
        table.row_start_index is not None
        and table.row_end_index is not None
        and table.row_end_index - table.row_start_index + 1 <= batch_size
        for batch in fragmented
        for table in batch.tables
    )
