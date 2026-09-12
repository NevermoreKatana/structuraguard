"""Единый protocol contract для всех built-ins и custom implementation."""

import pytest
from tests.fakes.normalization import PrefixNormalizer

from structuraguard.contracts.common import RawScalar, StringScalar
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizationStep,
    NormalizerParameter,
    NormalizerSpec,
)
from structuraguard.normalization.builtins import builtin_normalizers
from structuraguard.ports.normalization import Normalizer


@pytest.mark.parametrize(
    "normalizer",
    [*(normalizer for _, normalizer in builtin_normalizers()), PrefixNormalizer()],
)
def test_normalizer_protocol_is_deterministic_immutable_and_has_connected_trace(
    normalizer: Normalizer,
) -> None:
    assert isinstance(normalizer, Normalizer)
    policy = NormalizationPolicy()
    raw: RawScalar = StringScalar(value="true")
    parameters: tuple[NormalizerParameter, ...] = ()
    if isinstance(normalizer, PrefixNormalizer):
        parameters = (
            NormalizerParameter(name="prefix", value=StringScalar(value="tag:")),
        )
    before = raw.model_dump_json(), policy.model_dump_json()
    first = normalizer.normalize(raw, policy=policy, parameters=parameters)
    second = normalizer.normalize(raw, policy=policy, parameters=parameters)
    assert first == second
    assert (raw.model_dump_json(), policy.model_dump_json()) == before
    trace = NormalizationStep(
        spec=NormalizerSpec(normalizer_id="contract", parameters=parameters),
        input_value=raw,
        output=first,
    )
    assert trace.output == first
