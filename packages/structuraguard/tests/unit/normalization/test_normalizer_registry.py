"""Normalizer registration, custom config, snapshots и selected values."""

from typing import cast

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.fakes.normalization import PrefixNormalizer

from structuraguard.contracts.common import (
    PhysicalObjectKind,
    PhysicalSourceRef,
    RawScalar,
    SourceArtifactRef,
    StringScalar,
)
from structuraguard.contracts.execution import (
    PhysicalValueOrigin,
    SelectionOperation,
    SelectionTrace,
)
from structuraguard.contracts.normalization import (
    NormalizationPolicy,
    NormalizerDescriptor,
    NormalizerOutput,
    NormalizerParameter,
    NormalizerSpec,
)
from structuraguard.contracts.normalized import NormalizedValue
from structuraguard.contracts.profiling import LocalePolicy
from structuraguard.contracts.source import LineRangeLocation
from structuraguard.exceptions import ValidationError
from structuraguard.normalization import NormalizerRegistry
from structuraguard.ports.normalization import Normalizer


def test_custom_parameters_are_explicit_and_trace_keeps_every_transformation() -> None:
    registry = NormalizerRegistry.with_builtins()
    registry.register(
        NormalizerDescriptor(normalizer_id="custom.prefix"), PrefixNormalizer()
    )
    spec = NormalizerSpec(
        normalizer_id="custom.prefix",
        parameters=(
            NormalizerParameter(name="prefix", value=StringScalar(value="literal: ")),
        ),
    )
    snapshot = registry.freeze()
    result = snapshot.normalize(StringScalar(value="value"), steps=(spec,))
    assert result.accepted
    assert result.normalized_value == StringScalar(value="literal: value")
    assert result.raw_value == StringScalar(value="value")
    assert result.steps[0].spec == spec
    assert result.steps[0].output.transformations[0].operation == "add_prefix"
    assert (
        result.fingerprint
        == snapshot.normalize(StringScalar(value="value"), steps=(spec,)).fingerprint
    )


def test_snapshot_does_not_change_after_registration_and_freeze_rejects_writes() -> (
    None
):
    registry = NormalizerRegistry()
    old = registry.snapshot()
    fingerprint = old.fingerprint
    descriptor = NormalizerDescriptor(normalizer_id="custom.prefix")
    registry.register(descriptor, PrefixNormalizer())
    assert old.descriptors == ()
    assert old.fingerprint == fingerprint
    assert registry.snapshot().fingerprint != fingerprint
    with pytest.raises(ValidationError) as duplicate:
        registry.register(descriptor, PrefixNormalizer())
    assert duplicate.value.error_code == "NORMALIZER_DUPLICATE_REGISTRATION"
    registry.freeze()
    with pytest.raises(ValidationError) as frozen:
        registry.register(
            NormalizerDescriptor(normalizer_id="other"), PrefixNormalizer()
        )
    assert frozen.value.error_code == "NORMALIZER_REGISTRY_FROZEN"


def test_registration_order_does_not_change_fingerprint_and_versions_are_exact() -> (
    None
):
    first, second = NormalizerRegistry(), NormalizerRegistry()
    descriptors = (
        NormalizerDescriptor(normalizer_id="custom.prefix", version="1.0.0"),
        NormalizerDescriptor(normalizer_id="custom.prefix", version="2.0.0"),
    )
    for descriptor in descriptors:
        first.register(descriptor, PrefixNormalizer())
    for descriptor in reversed(descriptors):
        second.register(descriptor, PrefixNormalizer())
    assert first.snapshot().fingerprint == second.snapshot().fingerprint
    with pytest.raises(ValidationError) as unknown:
        first.snapshot().normalize(
            StringScalar(value="text"),
            steps=(NormalizerSpec(normalizer_id="custom.prefix", version="3.0.0"),),
        )
    assert unknown.value.error_code == "NORMALIZER_NOT_FOUND"


class AsyncNormalizer:
    async def normalize(
        self,
        value: RawScalar,
        *,
        policy: NormalizationPolicy,
        parameters: tuple[NormalizerParameter, ...],
    ) -> NormalizerOutput:
        return NormalizerOutput(value=value)


def test_async_implementation_is_rejected_during_registration() -> None:
    # Намеренно нарушаем static protocol для проверки runtime registration boundary.
    invalid = cast(Normalizer, AsyncNormalizer())
    with pytest.raises(ValidationError) as failed:
        NormalizerRegistry().register(
            NormalizerDescriptor(normalizer_id="async"), invalid
        )
    assert failed.value.error_code == "NORMALIZER_INVALID_ADAPTER"


def test_falsey_invalid_policy_does_not_select_defaults() -> None:
    registry = NormalizerRegistry.with_builtins().snapshot()
    # Невалидный runtime input не должен превращаться в permissive default.
    invalid = cast(NormalizationPolicy, False)
    with pytest.raises(ValidationError) as failed:
        registry.normalize(
            StringScalar(value="x"),
            steps=(NormalizerSpec(normalizer_id="trim"),),
            policy=invalid,
        )
    assert failed.value.error_code == "NORMALIZATION_POLICY_INVALID"


def selected_value() -> NormalizedValue:
    source = SourceArtifactRef(
        artifact_id="source-1", source_fingerprint="sha256:" + "a" * 64
    )
    ref = PhysicalSourceRef(
        extraction_id="extraction-1",
        batch_index=0,
        kind=PhysicalObjectKind.LINE,
        local_id="line-1",
    )
    raw = StringScalar(value="name=  Ada  ")
    return NormalizedValue(
        value_id="v-1",
        field_name="name",
        raw_value=raw,
        normalized_value=StringScalar(value="  Ada  "),
        semantic_type="string",
        source_refs=(ref,),
        transformations=("select_value",),
        origins=(
            PhysicalValueOrigin(
                source_ref=ref,
                raw_value=raw,
                location=LineRangeLocation(source=source, line_start=1, line_end=1),
            ),
        ),
        selection=SelectionTrace(
            operation=SelectionOperation.SELECT_VALUE,
            selector_fingerprint="sha256:" + "b" * 64,
        ),
    )


def test_normalize_value_preserves_raw_parent_selection_and_provenance() -> None:
    value = selected_value()
    before = value.model_dump_json()
    result = (
        NormalizerRegistry.with_builtins()
        .snapshot()
        .normalize_value(value, steps=(NormalizerSpec(normalizer_id="trim"),))
    )
    assert result.normalized_value == StringScalar(value="Ada")
    assert result.raw_value.value == "name=  Ada  "
    assert result.input_value.value == "  Ada  "
    assert result.source_value == value
    assert result.source_value is not value
    assert value.model_dump_json() == before
    assert value.transformations == ("select_value",)
    with pytest.raises(PydanticValidationError):
        result.raw_value.value = "overwrite"
    assert "Ada" not in repr(result)


def test_policy_is_immutable_and_local_to_call() -> None:
    policy = NormalizationPolicy()
    with pytest.raises(PydanticValidationError):
        policy.currency = "USD"
    registry = NormalizerRegistry.with_builtins().snapshot()
    steps = (NormalizerSpec(normalizer_id="date"),)
    assert not registry.normalize(
        StringScalar(value="01/02/2026"), steps=steps
    ).accepted
    assert registry.normalize(
        StringScalar(value="01/02/2026"),
        steps=steps,
        policy=NormalizationPolicy(locale=LocalePolicy.EN_US),
    ).accepted
    assert not registry.normalize(
        StringScalar(value="01/02/2026"), steps=steps
    ).accepted
