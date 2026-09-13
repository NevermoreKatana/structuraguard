"""M14 review: ошибки внешнего source transport не раскрывают restricted input."""

import traceback
from uuid import UUID

import pytest
from tests.unit.parsers.builtin._support import source_for

from structuraguard.contracts.security import SecurityPolicy
from structuraguard.exceptions import SecurityPolicyError, SourceError
from structuraguard.security import SecuritySession


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["typed", "lookup"])
@pytest.mark.parametrize("boundary", ["snapshot", "guarded_reader"])
async def test_source_transport_errors_are_closed_and_secret_safe(
    kind: str, boundary: str
) -> None:
    source = source_for(b"x", display_name="fixture.txt")

    class Reader:
        source_fingerprint = source.source_fingerprint

        async def read(self, size: int, *, offset: int = 0) -> bytes:
            if kind == "typed":
                raise SourceError(
                    error_code="SOURCE_READ_FAILED", message="restricted-input-canary"
                )
            raise KeyError("restricted-input-canary")

    run = SecuritySession(SecurityPolicy(), run_id=UUID(int=1))
    with pytest.raises(
        SecurityPolicyError, match="SECURITY_OPERATION_FAILED"
    ) as caught:
        if boundary == "snapshot":
            await run.snapshot(Reader(), kind="stream")
        else:
            await run.guarded_reader(Reader(), source).read(offset=0, size=1)
    assert "restricted-input-canary" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert len(run.events) == 1 and run.events[0].outcome == "failed"
    assert "restricted-input-canary" not in run.events[0].canonical_json()


@pytest.mark.anyio
async def test_reader_fingerprint_error_is_secret_safe() -> None:
    class Reader:
        @property
        def source_fingerprint(self) -> str:
            raise KeyError("restricted-fingerprint-canary")

        async def read(self, *, offset: int, size: int) -> bytes:
            raise AssertionError("fingerprint admission failed before read")

    run = SecuritySession(SecurityPolicy(), run_id=UUID(int=1))
    source = source_for(b"x", display_name="fixture.txt")
    with pytest.raises(
        SecurityPolicyError, match="SECURITY_OPERATION_FAILED"
    ) as caught:
        await run.guarded_reader(Reader(), source).read(offset=0, size=1)
    assert "restricted-fingerprint-canary" not in "".join(
        traceback.format_exception(caught.value)
    )
    assert run.events[0].outcome == "failed"
