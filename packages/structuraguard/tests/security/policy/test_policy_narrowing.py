"""A1: произвольное сужение maxima сохраняет permissions и immutable snapshot."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from structuraguard.contracts.security import SecurityLimits, SecurityPolicy


@given(st.lists(st.integers(min_value=1, max_value=30), min_size=16, max_size=16))
def test_narrowing_all_caps_preserves_authority_and_original(values: list[int]) -> None:
    limits = SecurityLimits.model_validate(
        dict.fromkeys(SecurityLimits.model_fields, 30)
    )
    original = SecurityPolicy(
        limits=limits,
        allowed_formats=("txt", "json"),
        parser_trust="trusted",
        strict_mode=True,
        risky_formats=("txt",),
    )
    fingerprint = original.fingerprint
    requested = SecurityLimits.model_validate(
        dict(zip(SecurityLimits.model_fields, values, strict=True))
    )
    narrowed = original.narrow(requested)
    assert narrowed.limits == requested
    assert narrowed.model_dump(exclude={"limits"}) == original.model_dump(
        exclude={"limits"}
    )
    assert original.limits == limits and original.fingerprint == fingerprint
    with pytest.raises(ValidationError):
        narrowed.limits.max_records = 31


@pytest.mark.parametrize("field", tuple(SecurityLimits.model_fields))
def test_one_expanding_cap_rejects_otherwise_narrower_policy(field: str) -> None:
    limits = SecurityLimits.model_validate(
        dict.fromkeys(SecurityLimits.model_fields, 30)
    )
    proposed = dict.fromkeys(SecurityLimits.model_fields, 1)
    proposed[field] = 31
    with pytest.raises(ValueError, match="Per-run limits"):
        SecurityPolicy(limits=limits).narrow(SecurityLimits.model_validate(proposed))
