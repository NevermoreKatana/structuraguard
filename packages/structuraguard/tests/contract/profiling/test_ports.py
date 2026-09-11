"""Один contract suite для concrete реализаций каждого M8 protocol."""

import pytest
from tests.fakes.profiling import normalized_stream

from structuraguard.contracts.common import DataClassification, IntegerScalar
from structuraguard.contracts.normalized import SemanticFieldRef
from structuraguard.contracts.profiling import (
    ExamplePolicy,
    NormalizedDataProfile,
    NormalizedProfilingOptions,
    PIIClassificationRequest,
    PIIClassificationResult,
)
from structuraguard.ports.profiling import NormalizedDataProfiler as ProfilerPort
from structuraguard.ports.security import PIIClassifier
from structuraguard.profiling import LocalPIIClassifier, NormalizedDataProfiler

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("implementation", [NormalizedDataProfiler])
async def test_profiler_contract_roundtrip_and_reuse(
    implementation: type[NormalizedDataProfiler],
) -> None:
    profiler = implementation()
    assert isinstance(profiler, ProfilerPort)
    result = await profiler.profile(normalized_stream([{"x": IntegerScalar(value=4)}]))
    assert NormalizedDataProfile.model_validate_json(result.model_dump_json()) == result
    empty = await profiler.profile(normalized_stream([]))
    assert empty.fields == ()


@pytest.mark.parametrize("implementation", [LocalPIIClassifier])
async def test_pii_port_binding_and_monotonicity(
    implementation: type[LocalPIIClassifier],
) -> None:
    classifier = implementation()
    assert isinstance(classifier, PIIClassifier)
    request = PIIClassificationRequest(
        field=SemanticFieldRef(entity_type="row", field_name="email"),
        checked_count=5,
        skipped_count=0,
        value_categories=("email",),
        minimum_classification=DataClassification.RESTRICTED,
    )
    result = await classifier.classify(request)
    assert result.input_fingerprint == request.input_fingerprint
    assert (
        result.classification == DataClassification.RESTRICTED
        and result.state == "detected"
    )


def test_options_reject_inconsistent_sample_budgets() -> None:
    with pytest.raises(ValueError):
        NormalizedProfilingOptions(examples_per_field=16)


def test_pii_categories_cannot_claim_a_permissive_classification() -> None:
    from structuraguard.contracts.profiling import PIIClassificationResult

    with pytest.raises(ValueError):
        PIIClassificationResult(
            field=SemanticFieldRef(entity_type="row", field_name="x"),
            input_fingerprint="sha256:" + "0" * 64,
            state="detected",
            categories=("credential",),
            classification=DataClassification.INTERNAL,
            complete=True,
        )


@pytest.mark.parametrize("implementation", [LocalPIIClassifier])
@pytest.mark.parametrize("floor", list(DataClassification))
async def test_pii_contract_findings_only_raise_the_callers_classification(
    implementation: type[LocalPIIClassifier],
    floor: DataClassification,
) -> None:
    classifier = implementation()
    levels = list(DataClassification)
    previous = levels.index(floor)
    for categories in ((), ("email",), ("email", "credential")):
        request = PIIClassificationRequest(
            field=SemanticFieldRef(entity_type="row", field_name="x"),
            checked_count=20,
            skipped_count=0,
            minimum_classification=floor,
            value_categories=categories,
        )
        result = await classifier.classify(request)
        current = levels.index(result.classification)
        assert current >= previous
        assert set(categories) <= set(result.categories)
        assert result.input_fingerprint == request.input_fingerprint
        previous = current


async def test_classifier_port_receives_aggregates_without_raw_examples() -> None:
    requests: list[PIIClassificationRequest] = []

    class Recording:
        async def classify(
            self, request: PIIClassificationRequest
        ) -> PIIClassificationResult:
            requests.append(request)
            return await LocalPIIClassifier().classify(request)

    from structuraguard.contracts.common import StringScalar

    canary = "private.person@example.org"
    result = await NormalizedDataProfiler(
        NormalizedProfilingOptions(examples=ExamplePolicy.LOCAL_RAW),
        classifier=Recording(),
    ).profile(normalized_stream({"x": StringScalar(value=canary)} for _ in range(100)))
    assert len(requests) == 1
    request = requests[0]
    assert request.checked_count == 100 and request.skipped_count == 0
    assert "email" in request.value_categories
    assert canary not in request.canonical_json()
    assert result.fields[0].pii.state == "detected"
    assert all(example.masked for example in result.fields[0].examples)
