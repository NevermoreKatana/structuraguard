"""M12 AC-01: повтор canonical output не создаёт новых transformations."""

import pytest

from structuraguard.contracts.common import StringScalar
from structuraguard.contracts.normalization import NormalizationPolicy, NormalizerSpec
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.normalization import NormalizerRegistry


@pytest.mark.parametrize(
    "name,raw",
    [
        ("trim", "  ООО Альфа  "),
        ("empty_to_null", ""),
        ("boolean", "ДА"),
        ("integer", "1250"),
        ("decimal", "1 250,50"),
        ("money", "1 250,50 ₽"),
        ("date", "02.09.2026"),
        ("datetime", "02.09.2026 15:30:00+03:00"),
        ("phone", "+7 (999) 123-45-67"),
        ("email", "User+tag@EXAMPLE.COM"),
        ("uuid", "550E8400E29B41D4A716446655440000"),
    ],
)
def test_every_builtin_accepts_its_output_without_another_change(
    name: str, raw: str
) -> None:
    registry = NormalizerRegistry.with_builtins().freeze()
    steps = (NormalizerSpec(normalizer_id=name),)
    policy = NormalizationPolicy(locale=LocalePolicy.RU_RU, currency="RUB")
    first = registry.normalize(StringScalar(value=raw), steps=steps, policy=policy)
    before = first.canonical_json()
    assert first.accepted
    second = registry.normalize(first.normalized_value, steps=steps, policy=policy)
    assert second.accepted
    assert (
        second.normalized_value.model_dump_json()
        == first.normalized_value.model_dump_json()
    )
    assert not any(step.output.transformations for step in second.steps)
    assert first.canonical_json() == before
    assert first.raw_value == StringScalar(value=raw)
