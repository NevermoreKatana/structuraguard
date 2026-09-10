"""Общий контракт SQLite/PostgreSQL inspection-only implementations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast

import pytest

from structuraguard.contracts import (
    DatabaseCatalog,
    DatabaseInspectionRequest,
    LoadContext,
    NormalizedBatch,
    ValidatedMappingPlan,
)
from structuraguard.domain import build_dependency_graph, database_fingerprint
from structuraguard.exceptions import (
    DatabaseInspectionError,
    OperationNotImplementedError,
)
from structuraguard.ports.database import DatabaseAdapter


async def assert_inspection_contract(
    adapter: DatabaseAdapter, request: DatabaseInspectionRequest
) -> DatabaseCatalog:
    assert isinstance(adapter, DatabaseAdapter)
    result = await adapter.inspect(request)
    assert result == await adapter.inspect(request)
    assert DatabaseCatalog.model_validate_json(result.model_dump_json()) == result
    assert (
        result.schema_version == "1.1.0" and result.fingerprint_version == "catalog-v1"
    )
    assert result.target_id == request.target_id
    assert result.target_policy_fingerprint == request.target_policy_fingerprint
    assert result.database_fingerprint == database_fingerprint(result)
    assert result.dependency_graph == build_dependency_graph(result)
    return result


async def assert_execute_unavailable(adapter: DatabaseAdapter) -> None:
    class Batches:
        def __aiter__(self) -> AsyncIterator[NormalizedBatch]:
            raise AssertionError("batches must not be consumed")

    with pytest.raises(OperationNotImplementedError) as caught:
        await adapter.execute(
            Batches(), cast(ValidatedMappingPlan, None), cast(LoadContext, None)
        )
    assert caught.value.error_code == "SDK_OPERATION_NOT_IMPLEMENTED"


async def assert_request_rejection(
    adapter: DatabaseAdapter, request: DatabaseInspectionRequest
) -> None:
    """Одинаковый negative contract полного inspect, включая unchecked DTO copy."""
    for updates, code in (
        ({"target_id": "outside"}, "DATABASE_TARGET_MISMATCH"),
        (
            {"target_policy_fingerprint": "sha256:" + "f" * 64},
            "DATABASE_POLICY_MISMATCH",
        ),
        ({"schema_names": ("outside",)}, "TARGET_NOT_ALLOWED"),
        ({"read_only": False}, "TARGET_NOT_ALLOWED"),
    ):
        with pytest.raises(DatabaseInspectionError) as caught:
            await adapter.inspect(request.model_copy(update=updates))
        assert caught.value.error_code == code
