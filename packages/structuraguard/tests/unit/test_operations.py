"""Facade guards и совместимые aliases после замены M1 stubs."""

import gc
import warnings
from typing import Never

import pytest
from tests.fakes.pipeline import request

from structuraguard import (
    AsyncStructuraGuard,
    OperationNotImplementedError,
    StructuraGuard,
    StructuraGuardError,
)
from structuraguard.pipeline import SourceRequest


@pytest.mark.anyio
async def test_schema_proposal_remains_explicitly_unsupported() -> None:
    with pytest.raises(OperationNotImplementedError) as captured:
        await AsyncStructuraGuard().propose_schema()
    assert captured.value.details["operation"] == "propose_schema"
    assert not captured.value.retryable


def test_sync_schema_proposal_remains_explicitly_unsupported() -> None:
    with pytest.raises(OperationNotImplementedError):
        StructuraGuard().propose_schema()


def test_sync_delegates_typed_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SourceRequest] = []

    async def delegated(self: AsyncStructuraGuard, source: SourceRequest) -> Never:
        calls.append(source)
        raise OperationNotImplementedError("inspect_source")

    monkeypatch.setattr(AsyncStructuraGuard, "inspect_source", delegated)
    source = request()
    with pytest.raises(OperationNotImplementedError):
        StructuraGuard().inspect_source(source)
    assert calls == [source]


@pytest.mark.anyio
async def test_all_sync_guards_precede_coroutine_creation() -> None:
    sdk = StructuraGuard()
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", RuntimeWarning)
        for name in (
            "inspect_source",
            "analyze_structure",
            "create_parse_plan",
            "validate_parse_plan",
            "parse_semantically",
            "profile_records",
            "create_mapping_plan",
            "validate_mapping_plan",
            "execute",
            "ingest",
            "analyze",
            "create_plan",
            "validate_plan",
        ):
            kwargs = (
                {"plan": None}
                if name
                in {
                    "validate_parse_plan",
                    "parse_semantically",
                    "validate_mapping_plan",
                    "validate_plan",
                    "execute",
                }
                else {}
            )
            with pytest.raises(StructuraGuardError, match="SYNC_API_IN_ASYNC_CONTEXT"):
                getattr(sdk, name)(request(), **kwargs)
        with pytest.raises(StructuraGuardError, match="SYNC_API_IN_ASYNC_CONTEXT"):
            sdk.inspect_database()
        with pytest.raises(StructuraGuardError, match="SYNC_API_IN_ASYNC_CONTEXT"):
            sdk.propose_schema()
        gc.collect()
    assert not [w for w in recorded if "was never awaited" in str(w.message)]


def test_mapping_aliases_keep_one_implementation() -> None:
    assert AsyncStructuraGuard.create_plan is AsyncStructuraGuard.create_mapping_plan
    assert StructuraGuard.validate_plan is StructuraGuard.validate_mapping_plan
