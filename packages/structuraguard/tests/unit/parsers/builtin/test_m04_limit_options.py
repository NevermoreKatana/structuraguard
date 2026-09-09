"""Непокрытые публичные limits A/C/D/E/F: finite, strict и immutable overrides."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal
from typing import cast

import pytest

from structuraguard.parsers.builtin import (
    DocxParserLimits,
    HtmlParserLimits,
    JsonParserLimits,
    LogParserLimits,
    MarkdownParserLimits,
    PdfParserLimits,
    TextParserLimits,
    XlsxParserLimits,
    XmlParserLimits,
    YamlParserLimits,
)
from structuraguard.parsers.tika import TikaParserLimits

type Limits = (
    TextParserLimits
    | JsonParserLimits
    | XmlParserLimits
    | HtmlParserLimits
    | YamlParserLimits
    | XlsxParserLimits
    | PdfParserLimits
    | DocxParserLimits
    | TikaParserLimits
)


@pytest.mark.parametrize(
    "limit_type",
    [
        TextParserLimits,
        LogParserLimits,
        MarkdownParserLimits,
        JsonParserLimits,
        XmlParserLimits,
        HtmlParserLimits,
        YamlParserLimits,
        XlsxParserLimits,
        PdfParserLimits,
        DocxParserLimits,
        TikaParserLimits,
    ],
)
@pytest.mark.parametrize(
    "invalid", [-1, True, 2**63], ids=["negative", "bool", "unbounded"]
)
def test_public_limit_overrides_are_strict_finite_and_frozen(
    limit_type: type[Limits], invalid: int | bool
) -> None:
    limits = limit_type()
    for item in fields(limits):
        value = getattr(limits, item.name)
        if type(value) not in {int, float, Decimal}:
            continue
        assert math.isfinite(value) and value >= 0, item.name
        with pytest.raises(ValueError):
            # Отрицательный corpus намеренно нарушает typed kwargs dataclass.
            cast(Callable[..., object], replace)(limits, **{item.name: invalid})
        with pytest.raises(FrozenInstanceError):
            setattr(limits, item.name, value)


@pytest.mark.parametrize(
    "limit_type",
    [XlsxParserLimits, PdfParserLimits, DocxParserLimits, TikaParserLimits],
)
@pytest.mark.parametrize("value", [0.0, float("inf"), float("nan")])
def test_processing_timeout_cannot_be_disabled_or_nonfinite(
    limit_type: type[
        XlsxParserLimits | PdfParserLimits | DocxParserLimits | TikaParserLimits
    ],
    value: float,
) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        replace(limit_type(), timeout_seconds=value)
