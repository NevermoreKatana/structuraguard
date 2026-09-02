from __future__ import annotations

import gc
import warnings
from collections.abc import Awaitable, Callable
from typing import Never

import pytest

from structuraguard import (
    AsyncStructuraGuard,
    OperationNotImplementedError,
    StructuraGuard,
    StructuraGuardError,
)

AsyncOperation = Callable[[], Awaitable[object]]
SyncOperation = Callable[[], object]


def _async_operations(
    sdk: AsyncStructuraGuard,
) -> tuple[tuple[str, AsyncOperation], ...]:
    return (
        ("inspect_source", sdk.inspect_source),
        ("inspect_database", sdk.inspect_database),
        ("create_plan", sdk.create_plan),
        ("validate_plan", sdk.validate_plan),
        ("execute", sdk.execute),
        ("analyze", sdk.analyze),
        ("ingest", sdk.ingest),
        ("propose_schema", sdk.propose_schema),
    )


def _sync_operations(sdk: StructuraGuard) -> tuple[tuple[str, SyncOperation], ...]:
    return (
        ("inspect_source", sdk.inspect_source),
        ("inspect_database", sdk.inspect_database),
        ("create_plan", sdk.create_plan),
        ("validate_plan", sdk.validate_plan),
        ("execute", sdk.execute),
        ("analyze", sdk.analyze),
        ("ingest", sdk.ingest),
        ("propose_schema", sdk.propose_schema),
    )


@pytest.mark.anyio
async def test_all_async_operations_fail_with_an_explicit_typed_error() -> None:
    sdk = AsyncStructuraGuard()

    for operation_name, operation in _async_operations(sdk):
        with pytest.raises(OperationNotImplementedError) as captured:
            await operation()

        assert captured.value.error_code == "SDK_OPERATION_NOT_IMPLEMENTED"
        assert captured.value.details["operation"] == operation_name
        assert captured.value.retryable is False


def test_all_sync_operations_fail_with_an_explicit_typed_error() -> None:
    sdk = StructuraGuard()

    for operation_name, operation in _sync_operations(sdk):
        with pytest.raises(OperationNotImplementedError) as captured:
            operation()

        assert captured.value.error_code == "SDK_OPERATION_NOT_IMPLEMENTED"
        assert captured.value.details["operation"] == operation_name
        assert captured.value.retryable is False


def test_sync_facade_delegates_arguments_to_the_async_facade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def delegated_inspect_source(
        self: AsyncStructuraGuard,
        *args: object,
        **kwargs: object,
    ) -> Never:
        calls.append((args, kwargs))
        raise OperationNotImplementedError("inspect_source")

    monkeypatch.setattr(
        AsyncStructuraGuard,
        "inspect_source",
        delegated_inspect_source,
    )
    source = object()

    with pytest.raises(OperationNotImplementedError):
        StructuraGuard().inspect_source(source, format_hint="txt")

    assert calls == [((source,), {"format_hint": "txt"})]


@pytest.mark.anyio
async def test_sync_operations_reject_an_active_event_loop_before_delegation() -> None:
    sdk = StructuraGuard()

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always", RuntimeWarning)
        for _operation_name, operation in _sync_operations(sdk):
            with pytest.raises(StructuraGuardError) as captured:
                operation()

            assert not isinstance(captured.value, OperationNotImplementedError)
            assert captured.value.error_code == "SYNC_API_IN_ASYNC_CONTEXT"

        gc.collect()

    assert not [
        warning
        for warning in recorded_warnings
        if issubclass(warning.category, RuntimeWarning)
        and "was never awaited" in str(warning.message)
    ]
