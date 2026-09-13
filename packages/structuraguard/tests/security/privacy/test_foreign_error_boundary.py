"""M14 review: полный error barrier для chunk transport и placeholder store."""

import traceback
from collections.abc import AsyncIterator

import pytest
from tests.fakes.privacy import READER, RUN, WRITER, protector, store_for

from structuraguard.contracts.privacy import DetectionPolicy, PlaceholderHandle
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.ports.privacy import PlaceholderPayload
from structuraguard.security.classification import ContentProtector
from structuraguard.security.redaction import restore_text


@pytest.mark.anyio
async def test_chunk_lookup_failure_does_not_escape_as_raw_exception() -> None:
    async def chunks() -> AsyncIterator[str]:
        yield "bounded"
        raise KeyError("restricted-chunk-canary")

    with pytest.raises(SecurityPolicyError, match="SECURITY_SCAN_FAILED") as caught:
        await ContentProtector(DetectionPolicy()).classify_chunks(chunks())
    assert "restricted-chunk-canary" not in "".join(
        traceback.format_exception(caught.value)
    )


@pytest.mark.anyio
async def test_restore_store_lookup_failure_does_not_reveal_raw_values() -> None:
    class BrokenStore:
        async def put(
            self,
            *,
            run_id: str,
            principal: str,
            payload: PlaceholderPayload,
            ttl_seconds: int,
        ) -> PlaceholderHandle:
            raise AssertionError("put is not used")

        async def get(
            self, *, handle: PlaceholderHandle, run_id: str, principal: str
        ) -> PlaceholderPayload:
            raise KeyError("restricted-map-canary")

        async def discard(
            self, *, handle: PlaceholderHandle, run_id: str, principal: str
        ) -> None:
            raise AssertionError("discard is not used")

    result = await protector().redact(
        "user@example.test", run_id=RUN, store=store_for(), principal=WRITER
    )
    with pytest.raises(SecurityPolicyError, match="SECURITY_MAP_INVALID") as caught:
        await restore_text(result, store=BrokenStore(), run_id=RUN, principal=READER)
    assert "restricted-map-canary" not in "".join(
        traceback.format_exception(caught.value)
    )
