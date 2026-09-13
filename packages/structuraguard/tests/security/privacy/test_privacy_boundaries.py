"""N/N+1, safe summaries, cancellation и неполные/обфусцированные данные."""

import asyncio
import json
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from functools import partial
from pathlib import Path
from uuid import UUID

import pytest

from structuraguard.contracts.privacy import (
    DetectionPolicy,
    DetectionReport,
    ScanLimits,
)
from structuraguard.exceptions import SecurityPolicyError
from structuraguard.security import SecurityPolicy, SecuritySession
from structuraguard.security.classification import ContentProtector


@pytest.mark.anyio
async def test_documented_false_positive_and_false_negative_fixtures() -> None:
    fixtures: object = json.loads(
        (Path(__file__).parents[2] / "fixtures/privacy/precision.json").read_text()
    )
    assert isinstance(fixtures, list)
    protector = ContentProtector(DetectionPolicy())
    for case in fixtures:
        assert isinstance(case, dict) and case["reason"]
        report = await protector.classify(case["text"])
        assert (
            case["category"] in {f.category.value for f in report.findings}
        ) is case["detected"], case["reason"]


@pytest.mark.anyio
async def test_match_survives_every_chunk_split() -> None:
    text = "a@b.test and password=synthetic-secret"
    protector = ContentProtector(DetectionPolicy())
    expected = await protector.classify(text)
    for split in range(len(text) + 1):

        async def chunks(position: int = split) -> AsyncIterator[str]:
            yield text[:position]
            yield text[position:]

        assert await protector.classify_chunks(chunks()) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["bytes", "fields", "findings", "work", "chunks"])
@pytest.mark.parametrize("extra", [0, 1])
async def test_scan_budget_exact_and_one_over(kind: str, extra: int) -> None:
    operation: Callable[[], Awaitable[DetectionReport]]
    if kind == "bytes":
        protector = ContentProtector(
            DetectionPolicy(limits=ScanLimits(max_bytes=3 - extra))
        )
        operation = partial(protector.classify, "éa")
    elif kind == "fields":
        protector = ContentProtector(
            DetectionPolicy(limits=ScanLimits(max_fields=2 - extra))
        )
        operation = partial(protector.classify_fields, {"a": "x", "b": "y"})
    elif kind == "findings":
        protector = ContentProtector(
            DetectionPolicy(limits=ScanLimits(max_findings=2 - extra))
        )
        operation = partial(protector.classify, "a@b.test c@d.test")
    elif kind == "work":
        baseline = await ContentProtector(DetectionPolicy()).classify("hello")
        protector = ContentProtector(
            DetectionPolicy(limits=ScanLimits(max_work=baseline.work_used - extra))
        )
        operation = partial(protector.classify, "hello")
    else:

        async def chunks() -> AsyncIterator[str]:
            yield "a"
            yield "b"

        protector = ContentProtector(
            DetectionPolicy(limits=ScanLimits(max_chunks=2 - extra))
        )
        operation = partial(protector.classify_chunks, chunks())
    if extra:
        with pytest.raises(SecurityPolicyError, match="SECURITY_LIMIT_EXCEEDED"):
            await operation()
    else:
        assert (await operation()).complete


@pytest.mark.anyio
async def test_cancelled_scan_has_safe_terminal_resource_event() -> None:
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def chunks() -> AsyncIterator[str]:
        yield "hello"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    run = SecuritySession(SecurityPolicy(), run_id=UUID(int=1))
    protector = ContentProtector(DetectionPolicy(), resources=run)
    task = asyncio.create_task(protector.classify_chunks(chunks()))
    await entered.wait()
    task.cancel("secret-canary")
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert cleaned.is_set() and caught.value.args == ()
    assert run.events[0].outcome == "cancelled"
    assert "secret-canary" not in run.events[0].canonical_json()


@pytest.mark.anyio
async def test_adapter_error_drops_raw_exception_chain() -> None:
    async def chunks() -> AsyncIterator[str]:
        yield "hello"
        raise OSError("password=secret-canary")

    with pytest.raises(SecurityPolicyError, match="SECURITY_SCAN_FAILED") as caught:
        await ContentProtector(DetectionPolicy()).classify_chunks(chunks())
    assert "secret-canary" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.anyio
async def test_escaped_password_and_nested_assignment_do_not_leak_suffix() -> None:
    protector = ContentProtector(DetectionPolicy())
    result = await protector.redact(
        r'password="first\"secret-canary"', run_id=UUID(int=1)
    )
    assert "secret-canary" not in result.text
    result = await protector.redact(
        "password=" * 500 + "secret-canary", run_id=UUID(int=1)
    )
    assert "secret-canary" not in result.text


@pytest.mark.anyio
async def test_unterminated_key_is_masked_to_eof() -> None:
    text = "prefix -----BEGIN PRIVATE KEY-----\nsecret-canary\nrest"
    result = await ContentProtector(DetectionPolicy()).redact(text, run_id=UUID(int=1))
    assert "secret-canary" not in result.text and "rest" not in result.text


@pytest.mark.anyio
async def test_reserved_placeholder_input_is_rejected() -> None:
    with pytest.raises(SecurityPolicyError, match="SECURITY_REDACTION_DENIED"):
        await ContentProtector(DetectionPolicy()).redact(
            "[SGR:forged]", run_id=UUID(int=1)
        )


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["SECURITY_MAP_INVALID", "SECRET_CANARY"])
async def test_adapter_typed_error_has_no_authority_over_diagnostics(code: str) -> None:
    async def chunks() -> AsyncIterator[str]:
        yield "hello"
        raise SecurityPolicyError(error_code=code, message="raw-sensitive-canary")

    with pytest.raises(SecurityPolicyError) as caught:
        await ContentProtector(DetectionPolicy()).classify_chunks(chunks())
    diagnostics = "".join(traceback.format_exception(caught.value))
    assert "raw-sensitive-canary" not in diagnostics
    assert "SECRET_CANARY" not in diagnostics
