"""Regression tests для typed parser errors на trusted adapter boundary."""

from __future__ import annotations

import asyncio
import traceback
from decimal import Decimal
from typing import Literal

import pytest
from tests.fakes.parsers import FakeParser, FakeSourceReader

from structuraguard.contracts import (
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.ports.source import ParseContext, ProbeContext

type BoundaryPhase = Literal["probe", "parse_call", "parse_iteration"]

_SOURCE_FINGERPRINT = "sha256:" + "a" * 64
_BOUNDARY_PHASES: tuple[BoundaryPhase, ...] = (
    "probe",
    "parse_call",
    "parse_iteration",
)
_ALLOWLISTED_ERROR_CODES = (
    "PARSER_MALFORMED_INPUT",
    "PARSER_UNSUPPORTED_FEATURE",
    "PARSER_ENCODING_UNSUPPORTED",
)
_PUBLIC_MESSAGES = {
    "PARSER_MALFORMED_INPUT": "Parser отклонил некорректный входной формат.",
    "PARSER_UNSUPPORTED_FEATURE": (
        "Parser не поддерживает обнаруженную возможность формата."
    ),
    "PARSER_ENCODING_UNSUPPORTED": (
        "Parser не может безопасно определить или декодировать кодировку."
    ),
}
_PUBLIC_DETAILS = {
    "PARSER_MALFORMED_INPUT": {"reason": "binary_content"},
    "PARSER_UNSUPPORTED_FEATURE": {
        "reason": "feature_disabled",
        "feature": "configured_template",
    },
    "PARSER_ENCODING_UNSUPPORTED": {"reason": "decode_failed"},
}


def _source() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="source-1",
        display_name="sample.txt",
        media_type="text/plain",
        size_bytes=6,
        source_fingerprint=_SOURCE_FINGERPRINT,
    )


def _contexts() -> tuple[ProbeContext, ParseContext]:
    reader = FakeSourceReader(_SOURCE_FINGERPRINT, content=b"sample")
    return (
        ProbeContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_probe_bytes=64,
        ),
        ParseContext(
            reader=reader,
            source_fingerprint=_SOURCE_FINGERPRINT,
            max_bytes=64,
            max_records=10,
            max_nesting_depth=4,
        ),
    )


def _supported_probe(source: SourceArtifact) -> ProbeResult:
    return ProbeResult(
        source=source.ref,
        adapter_id="fake.parser",
        adapter_version="1.0.0",
        supported=True,
        confidence=Decimal("1"),
        detected_media_type="text/plain",
        detected_encoding="utf-8",
        format_id="txt",
        signals=(
            ProbeSignal(
                kind=ProbeSignalKind.CONTENT_MEDIA_TYPE,
                outcome=ProbeSignalOutcome.MATCH,
            ),
        ),
    )


def _parser_error_with_hostile_chain(
    error_code: str,
    marker: str,
    *,
    details: dict[str, str | int] | None = None,
) -> ParserError:
    try:
        raise RuntimeError(marker)
    except RuntimeError as cause:
        error = ParserError(
            error_code=error_code,
            message=f"Недоверенный adapter message: {marker}",
            details=details
            if details is not None
            else {
                "reason": {
                    "PARSER_MALFORMED_INPUT": "binary_content",
                    "PARSER_UNSUPPORTED_FEATURE": "feature_disabled",
                    "PARSER_ENCODING_UNSUPPORTED": "decode_failed",
                }.get(error_code, "private_failure"),
                "encoding": "utf-8",
                "feature": "configured_template",
                "excerpt": marker,
                "line": 7,
            },
            run_id=marker,
            retryable=True,
            cause=cause,
        )
        error.add_note(marker)
        try:
            raise error from cause
        except ParserError as raised:
            return raised


def _security_error_with_hostile_chain(marker: str) -> SecurityPolicyError:
    try:
        raise RuntimeError(marker)
    except RuntimeError as cause:
        error = SecurityPolicyError(
            error_code="SECURITY_LIMIT_EXCEEDED",
            message="Parser превысил установленный предел.",
            details={"resource": "records", "password": marker},
            cause=cause,
        )
        error.add_note(marker)
        try:
            raise error from cause
        except SecurityPolicyError as raised:
            return raised


def _cancellation_with_hostile_chain(marker: str) -> asyncio.CancelledError:
    try:
        raise RuntimeError(marker)
    except RuntimeError as cause:
        cancellation = asyncio.CancelledError(marker)
        cancellation.add_note(marker)
        try:
            raise cancellation from cause
        except asyncio.CancelledError as raised:
            return raised


async def _raise_at_boundary(phase: BoundaryPhase, error: BaseException) -> None:
    source = _source()
    probe_context, parse_context = _contexts()
    if phase == "probe":
        parser = FakeParser(probe_error=error)
    elif phase == "parse_call":
        parser = FakeParser(
            probe_result=_supported_probe(source),
            parse_call_error=error,
        )
    else:
        parser = FakeParser(
            probe_result=_supported_probe(source),
            parse_error=error,
        )

    registry = ParserRegistry()
    registry.register(parser)
    async with registry.session() as session:
        if phase == "probe":
            await session.select(source, probe_context)
            return

        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)
        if phase == "parse_iteration":
            await anext(stream)


def _assert_detached_without_marker(error: BaseException, marker: str) -> None:
    rendered = " ".join(
        (
            str(error),
            repr(error),
            repr(error.__context__),
            repr(error.__cause__),
            repr(getattr(error, "__notes__", ())),
            "".join(traceback.format_exception(error)),
        )
    )
    assert error.__context__ is None
    assert error.__cause__ is None
    assert tuple(getattr(error, "__notes__", ())) == ()
    assert marker not in rendered


@pytest.mark.anyio
@pytest.mark.parametrize("phase", _BOUNDARY_PHASES)
@pytest.mark.parametrize("error_code", _ALLOWLISTED_ERROR_CODES)
async def test_allowlisted_parser_error_is_reconstructed_and_sanitized(
    phase: BoundaryPhase,
    error_code: str,
) -> None:
    marker = "DO_NOT_LEAK_ALLOWLISTED_ERROR"
    hostile = _parser_error_with_hostile_chain(error_code, marker)

    with pytest.raises(ParserError) as raised:
        await _raise_at_boundary(phase, hostile)

    error = raised.value
    assert error.error_code == error_code
    assert error is not hostile
    assert error.message == _PUBLIC_MESSAGES[error_code]
    assert error.details == _PUBLIC_DETAILS[error_code]
    assert error.run_id is None
    assert error.retryable is False
    assert error.cause is None
    _assert_detached_without_marker(error, marker)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", _BOUNDARY_PHASES)
@pytest.mark.parametrize("error_code", _ALLOWLISTED_ERROR_CODES)
async def test_allowlisted_parser_error_discards_unrecognized_detail_values(
    phase: BoundaryPhase,
    error_code: str,
) -> None:
    marker = "TOPSECRETRAWVALUE123"
    hostile = ParserError(
        error_code=error_code,
        message=marker,
        details={
            "reason": marker,
            "encoding": marker,
            "feature": marker,
            "excerpt": marker,
        },
        retryable=True,
        cause=RuntimeError(marker),
    )

    with pytest.raises(ParserError) as raised:
        await _raise_at_boundary(phase, hostile)

    error = raised.value
    assert error.error_code == error_code
    assert error.message == _PUBLIC_MESSAGES[error_code]
    assert error.details == {}
    assert error.retryable is False
    assert error.cause is None
    _assert_detached_without_marker(error, marker)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", _BOUNDARY_PHASES)
@pytest.mark.parametrize("error_code", _ALLOWLISTED_ERROR_CODES)
async def test_allowlisted_parser_error_in_base_group_is_reconstructed(
    phase: BoundaryPhase,
    error_code: str,
) -> None:
    marker = "DO_NOT_LEAK_ALLOWLISTED_GROUP"
    raw_value = "TOPSECRETRAWVALUE123"
    hostile = _parser_error_with_hostile_chain(
        error_code,
        marker,
        details={
            "reason": raw_value,
            "encoding": raw_value,
            "feature": raw_value,
            "excerpt": raw_value,
        },
    )
    group = BaseExceptionGroup(
        marker,
        (hostile, _cancellation_with_hostile_chain(marker)),
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        await _raise_at_boundary(phase, group)

    parser_errors = tuple(
        leaf for leaf in raised.value.exceptions if isinstance(leaf, ParserError)
    )
    assert len(parser_errors) == 1
    error = parser_errors[0]
    assert error is not hostile
    assert error.error_code == error_code
    assert error.message == _PUBLIC_MESSAGES[error_code]
    assert error.details == {}
    assert error.run_id is None
    assert error.retryable is False
    assert error.cause is None
    _assert_detached_without_marker(raised.value, marker)
    assert raw_value not in "".join(traceback.format_exception(raised.value))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("phase", "expected_code", "expected_reason"),
    (
        ("probe", "PARSER_PROBE_FAILED", "typed_probe_failure"),
        ("parse_call", "PARSER_OUTPUT_INVALID", "parse_call_failed"),
        ("parse_iteration", "PARSER_OUTPUT_INVALID", "parse_iteration_failed"),
    ),
)
async def test_non_allowlisted_parser_error_remains_fail_closed(
    phase: BoundaryPhase,
    expected_code: str,
    expected_reason: str,
) -> None:
    marker = "password=DO_NOT_LEAK_PRIVATE_ERROR"
    hostile = _parser_error_with_hostile_chain("PARSER_PRIVATE_FAILURE", marker)

    with pytest.raises(ParserError) as raised:
        await _raise_at_boundary(phase, hostile)

    error = raised.value
    assert error.error_code == expected_code
    assert error.details == {
        "adapter_id": "fake.parser",
        "reason": expected_reason,
    }
    assert error.cause == "ParserError"
    _assert_detached_without_marker(error, marker)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", _BOUNDARY_PHASES)
async def test_security_policy_error_keeps_existing_sanitized_semantics(
    phase: BoundaryPhase,
) -> None:
    marker = "password=DO_NOT_LEAK_SECURITY_ERROR"
    hostile = _security_error_with_hostile_chain(marker)

    with pytest.raises(SecurityPolicyError) as raised:
        await _raise_at_boundary(phase, hostile)

    error = raised.value
    assert error.error_code == "SECURITY_LIMIT_EXCEEDED"
    assert error.details["resource"] == "records"
    assert error.details["password"] == "[REDACTED]"
    assert error.cause == "RuntimeError"
    _assert_detached_without_marker(error, marker)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", _BOUNDARY_PHASES)
async def test_cancellation_keeps_existing_sanitized_control_flow(
    phase: BoundaryPhase,
) -> None:
    marker = "password=DO_NOT_LEAK_CANCELLATION"
    hostile = _cancellation_with_hostile_chain(marker)

    with pytest.raises(asyncio.CancelledError) as raised:
        await _raise_at_boundary(phase, hostile)

    cancellation = raised.value
    assert type(cancellation) is asyncio.CancelledError
    assert cancellation.args == ()
    _assert_detached_without_marker(cancellation, marker)
