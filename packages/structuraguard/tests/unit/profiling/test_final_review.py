"""Регрессии финального review: неверные timezone offsets и email domains."""

from datetime import UTC, datetime

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import StringScalar
from structuraguard.profiling import NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("offset", ["+00:60", "+00:99", "-01:60", "-23:99"])
async def test_invalid_offset_does_not_produce_datetime_evidence(offset: str) -> None:
    scalar = StringScalar(value="2026-01-01T12:00:00" + offset)
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": scalar}] * 20)
    )
    field = profile.fields[0]
    assert "datetime" not in {p.code for p in field.patterns}
    assert field.inference.inferred_type == "string"
    assert field.candidate_extrema == ()
    assert field.minimum == field.maximum == scalar
    assert field.non_null_count == field.pattern_checked == 20


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        ("+00:59", datetime(2026, 1, 1, 11, 1, tzinfo=UTC)),
        ("-23:59", datetime(2026, 1, 2, 11, 59, tzinfo=UTC)),
    ],
)
async def test_valid_offset_boundary_preserves_utc_evidence(
    offset: str, expected: datetime
) -> None:
    scalar = StringScalar(value="2026-01-01T12:00:00" + offset)
    profile = await NormalizedDataProfiler().profile(
        normalized_stream([{"x": scalar}] * 20)
    )
    field = profile.fields[0]
    assert field.inference.inferred_type == "datetime"
    assert field.candidate_extrema[0].minimum.value == expected
    assert field.minimum == scalar


@pytest.mark.parametrize(
    "domain", ["a..b.com", "a-.b.com", "a.-b.com", "a" * 64 + ".com"]
)
async def test_malformed_email_domain_cannot_be_a_natural_key(domain: str) -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream(
            {"x": StringScalar(value=f"person{i}@{domain}")} for i in range(20)
        )
    )
    field = profile.fields[0]
    assert "email" not in {p.code for p in field.patterns}
    assert field.inference.inferred_type == "string"
    assert field.identity.strength == "none"
    assert field.unique_count == field.non_null_count == 20
    assert "email" in field.pii.categories


@pytest.mark.parametrize(
    "domain", ["example.org", "a-b.example.org", "a" * 63 + ".org"]
)
async def test_valid_email_domain_retains_natural_key_evidence(domain: str) -> None:
    profile = await NormalizedDataProfiler().profile(
        normalized_stream(
            {"x": StringScalar(value=f"person{i}@{domain}")} for i in range(20)
        )
    )
    field = profile.fields[0]
    assert next(p for p in field.patterns if p.code == "email").count == 20
    assert field.inference.inferred_type == "identifier"
    assert field.identity.kind == "natural_key"
    assert field.identity.strength == "candidate"
