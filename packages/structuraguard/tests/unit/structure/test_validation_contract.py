"""Общий observable contract sync/async validation и versioned normalized DTO."""

import json

import pytest
from pydantic import ValidationError
from tests.unit.structure.test_execution import execute, prepared, stream

from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParsePlanValidationResult,
    ValidatedParsePlan,
)
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.structure import ParsePlanValidator


@pytest.mark.anyio
@pytest.mark.parametrize("representation", ["dto", "decoded_json"])
async def test_sync_async_validation_has_identical_binding_and_round_trips(
    representation: str,
) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    payload = (
        request if representation == "dto" else json.loads(request.model_dump_json())
    )
    validator = ParsePlanValidator()
    sync_result = validator.validate(payload, batches=batches)
    async_result = await validator.validate_source(payload, stream(batches))
    for result in (sync_result, async_result):
        assert result.validated_plan is not None
        assert result.validated_plan.plan == request.plan
        assert result.source_fingerprint == request.source.source_fingerprint
        assert result.extraction_fingerprint == request.manifest.extraction_fingerprint
        assert result.profile_fingerprint == request.profile.profile_fingerprint
        assert (
            ParsePlanValidationResult.model_validate_json(result.model_dump_json())
            == result
        )
        assert (
            ValidatedParsePlan.model_validate_json(
                result.validated_plan.model_dump_json()
            )
            == result.validated_plan
        )
    assert sync_result.validation_fingerprint == async_result.validation_fingerprint


@pytest.mark.anyio
@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "missing_origins",
        "copy_raw",
        "foreign_location",
        "unknown_operation",
        "legacy_version",
    ],
)
async def test_normalized_extensions_round_trip_and_reject_broken_provenance(
    mutation: str,
) -> None:
    request, batches = await prepared(DelimitedTextParser(), b"name,n\nAda,1\nBob,2\n")
    output = await execute(request, batches)
    for batch in output:
        assert NormalizedBatch.model_validate_json(batch.model_dump_json()) == batch
    if mutation == "none":
        return
    payload = output[0].model_dump(mode="python")
    payload["batch_fingerprint"] = "sha256:" + "0" * 64
    value = payload["records"][0]["entities"][0]["values"][0]
    if mutation == "missing_origins":
        value["origins"] = ()
    elif mutation == "copy_raw":
        value["origins"][0]["raw_value"]["value"] = "tampered"
    elif mutation == "foreign_location":
        value["origins"][0]["location"]["source"]["source_fingerprint"] = (
            "sha256:" + "f" * 64
        )
    elif mutation == "unknown_operation":
        value["selection"]["operation"] = "xpath"
    else:
        payload["schema_version"] = "1.0.0"
    with pytest.raises(ValidationError):
        NormalizedBatch.model_validate(payload)
