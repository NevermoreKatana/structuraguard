from __future__ import annotations

import asyncio
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from decimal import Decimal
from itertools import permutations
from typing import cast

import pytest
from tests.fakes.parsers import FakeParser

from structuraguard.contracts.source import (
    ExtractedBatch,
    ProbeResult,
    ProbeSignal,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.parsers import ParserRegistry
from structuraguard.ports import Parser
from structuraguard.ports.source import ParseContext, ProbeContext

_FINGERPRINT = "sha256:" + "a" * 64


class _Reader:
    source_fingerprint = _FINGERPRINT

    async def read(self, *, offset: int, size: int) -> bytes:
        return b"sample"[offset : offset + size]


class _SynchronousProbeParser:
    adapter_id = "parser.sync"
    version = "1.0.0"
    priority = 0

    def __init__(self, result: ProbeResult) -> None:
        self._result = result

    def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        del source, context
        return self._result

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        del source, context
        return self._empty()

    async def _empty(self) -> AsyncIterator[ExtractedBatch]:
        batches: tuple[ExtractedBatch, ...] = ()
        for batch in batches:
            yield batch


class _SynchronousProbeFailureParser(_SynchronousProbeParser):
    adapter_id = "parser.alpha"

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def probe(self, source: SourceArtifact, context: ProbeContext) -> ProbeResult:
        del source, context
        raise self._error


class _ContextRecordingParser(FakeParser):
    def __init__(self, *, probe_result: ProbeResult) -> None:
        super().__init__(probe_result=probe_result)
        self.parse_context: ParseContext | None = None

    def parse(
        self,
        source: SourceArtifact,
        context: ParseContext,
    ) -> AsyncIterator[ExtractedBatch]:
        self.parse_context = context
        return super().parse(source, context)


class _HostileProbeValue:
    def __repr__(self) -> str:
        return "password=DO_NOT_LEAK_PROBE_SCHEMA"


def _security_error_with_context(marker: str) -> SecurityPolicyError:
    try:
        raise RuntimeError(marker)
    except RuntimeError:
        try:
            security_error = SecurityPolicyError(
                error_code="SECURITY_LIMIT_EXCEEDED",
                message="Probe byte budget исчерпан.",
                details={"resource": "probe_bytes"},
            )
            security_error.add_note(marker)
            raise security_error
        except SecurityPolicyError as error:
            return error


def _cancellation_with_context(marker: str) -> asyncio.CancelledError:
    try:
        raise RuntimeError(marker)
    except RuntimeError as error:
        cancellation = asyncio.CancelledError(marker)
        cancellation.add_note(marker)
        try:
            raise cancellation from error
        except asyncio.CancelledError as raised:
            return raised


def _hostile_base_exception_group(marker: str) -> BaseExceptionGroup:
    failure = RuntimeError(marker)
    failure.add_note(marker)
    return BaseExceptionGroup(
        marker,
        (_cancellation_with_context(marker), failure),
    )


@pytest.fixture
def source() -> SourceArtifact:
    return SourceArtifact(
        artifact_id="source-1",
        display_name="orders.csv",
        media_type="text/plain",
        size_bytes=6,
        source_fingerprint=_FINGERPRINT,
    )


@pytest.fixture
def probe_context() -> ProbeContext:
    return ProbeContext(
        reader=_Reader(),
        source_fingerprint=_FINGERPRINT,
        max_probe_bytes=32,
    )


def _result(
    source: SourceArtifact,
    *,
    adapter_id: str,
    format_id: str,
    confidence: str,
    signals: tuple[ProbeSignal, ...],
) -> ProbeResult:
    return ProbeResult(
        source=source.ref,
        adapter_id=adapter_id,
        adapter_version="1.0.0",
        supported=True,
        confidence=Decimal(confidence),
        detected_media_type="text/csv",
        format_id=format_id,
        signals=signals,
    )


def _signal(kind: ProbeSignalKind, outcome: ProbeSignalOutcome) -> ProbeSignal:
    return ProbeSignal(kind=kind, outcome=outcome)


def _recording_probe(
    calls: list[str],
    *,
    adapter_id: str,
    confidence: str,
    signal_kind: ProbeSignalKind,
) -> Callable[[SourceArtifact, ProbeContext], Awaitable[ProbeResult]]:
    async def probe(
        probed_source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del context
        calls.append(adapter_id)
        return _result(
            probed_source,
            adapter_id=adapter_id,
            format_id="csv",
            confidence=confidence,
            signals=(_signal(signal_kind, ProbeSignalOutcome.MATCH),),
        )

    return probe


@pytest.mark.anyio
async def test_content_evidence_precedes_score_priority_and_registration_order(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    signature = FakeParser(
        adapter_id="parser.signature",
        version="1.0.0",
        priority=-10,
        probe_result=_result(
            source,
            adapter_id="parser.signature",
            format_id="csv",
            confidence="0.40",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        ),
    )
    mime_only = FakeParser(
        adapter_id="parser.mime",
        version="1.0.0",
        priority=100,
        probe_result=_result(
            source,
            adapter_id="parser.mime",
            format_id="csv",
            confidence="0.99",
            signals=(
                _signal(
                    ProbeSignalKind.CONTENT_MEDIA_TYPE,
                    ProbeSignalOutcome.MATCH,
                ),
            ),
        ),
    )
    registry = ParserRegistry()
    registry.register_many((mime_only, signature))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "parser.signature"


@pytest.mark.anyio
async def test_registration_permutations_keep_probe_order_and_vector_winner(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    specifications = (
        (
            "parser.internal",
            -100,
            "0.10",
            ProbeSignalKind.INTERNAL_STRUCTURE,
        ),
        ("parser.signature", 100, "0.99", ProbeSignalKind.SIGNATURE),
        ("parser.mime", 2**31 - 1, "0.99", ProbeSignalKind.CONTENT_MEDIA_TYPE),
    )

    for registration_order in permutations(specifications):
        probe_order: list[str] = []
        parsers: list[FakeParser] = []
        for adapter_id, priority, confidence, signal_kind in registration_order:
            parsers.append(
                FakeParser(
                    adapter_id=adapter_id,
                    version="1.0.0",
                    priority=priority,
                    probe_factory=_recording_probe(
                        probe_order,
                        adapter_id=adapter_id,
                        confidence=confidence,
                        signal_kind=signal_kind,
                    ),
                )
            )

        registry = ParserRegistry()
        registry.register_many(parsers)
        async with registry.session() as session:
            selected = await session.select(source, probe_context)

        assert probe_order == sorted(item[0] for item in specifications)
        assert selected.adapter_id == "parser.internal"


@pytest.mark.anyio
async def test_empty_registry_is_typed_not_found(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    async with ParserRegistry().session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_NOT_FOUND"


@pytest.mark.anyio
async def test_all_normal_unsupported_results_are_typed_unsupported(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    registry = ParserRegistry()
    registry.register(FakeParser())

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FORMAT"


@pytest.mark.anyio
async def test_all_dependency_failures_remain_distinct_from_unsupported(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            probe_error=ParserError(
                error_code="PARSER_DEPENDENCY_UNAVAILABLE",
                message="Optional parser dependency отсутствует.",
                retryable=True,
            )
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert raised.value.retryable is True


@pytest.mark.anyio
async def test_unavailable_candidate_dominates_normal_unsupported_outcome(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    registry = ParserRegistry()
    registry.register_many(
        (
            FakeParser(adapter_id="parser.unsupported"),
            FakeParser(
                adapter_id="parser.unavailable",
                probe_error=ParserError(
                    error_code="PARSER_DEPENDENCY_UNAVAILABLE",
                    message="Optional parser dependency отсутствует.",
                ),
            ),
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_DEPENDENCY_UNAVAILABLE"
    assert raised.value.details["unavailable_count"] == 1


@pytest.mark.anyio
async def test_supported_candidate_wins_when_other_dependency_is_unavailable(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    supported = FakeParser(
        adapter_id="parser.supported",
        version="1.0.0",
        priority=0,
        probe_result=_result(
            source,
            adapter_id="parser.supported",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        ),
    )
    unavailable = FakeParser(
        adapter_id="parser.unavailable",
        version="1.0.0",
        priority=100,
        probe_error=ParserError(
            error_code="PARSER_DEPENDENCY_UNAVAILABLE",
            message="Optional parser dependency отсутствует.",
        ),
    )
    registry = ParserRegistry()
    registry.register_many((unavailable, supported))

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "parser.supported"


@pytest.mark.anyio
async def test_non_awaitable_probe_result_is_typed_invalid(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = ProbeResult(
        source=source.ref,
        adapter_id="parser.sync",
        adapter_version="1.0.0",
        supported=False,
        confidence=Decimal("0"),
    )
    registry = ParserRegistry()
    registry.register(cast(Parser, _SynchronousProbeParser(result)))

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_PROBE_INVALID"
    assert raised.value.details["reason"] == "probe_not_awaitable"


@pytest.mark.anyio
async def test_score_then_priority_then_adapter_id_are_deterministic_ties(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    signal = (_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),)
    parsers = (
        FakeParser(
            adapter_id="parser.low",
            version="1.0.0",
            priority=100,
            probe_result=_result(
                source,
                adapter_id="parser.low",
                format_id="csv",
                confidence="0.80",
                signals=signal,
            ),
        ),
        FakeParser(
            adapter_id="parser.aaa",
            version="1.0.0",
            priority=10,
            probe_result=_result(
                source,
                adapter_id="parser.aaa",
                format_id="csv",
                confidence="0.90",
                signals=signal,
            ),
        ),
        FakeParser(
            adapter_id="parser.zeta",
            version="1.0.0",
            priority=20,
            probe_result=_result(
                source,
                adapter_id="parser.zeta",
                format_id="csv",
                confidence="0.90",
                signals=signal,
            ),
        ),
        FakeParser(
            adapter_id="parser.alpha",
            version="1.0.0",
            priority=20,
            probe_result=_result(
                source,
                adapter_id="parser.alpha",
                format_id="csv",
                confidence="0.90",
                signals=signal,
            ),
        ),
    )
    registry = ParserRegistry()
    registry.register_many(parsers)

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "parser.alpha"


@pytest.mark.anyio
async def test_equal_strong_evidence_for_different_formats_is_typed_ambiguity(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    signal = (_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),)
    registry = ParserRegistry()
    registry.register_many(
        (
            FakeParser(
                adapter_id="parser.csv",
                version="1.0.0",
                priority=100,
                probe_result=_result(
                    source,
                    adapter_id="parser.csv",
                    format_id="csv",
                    confidence="0.99",
                    signals=signal,
                ),
            ),
            FakeParser(
                adapter_id="parser.tsv",
                version="1.0.0",
                priority=1,
                probe_result=_result(
                    source,
                    adapter_id="parser.tsv",
                    format_id="tsv",
                    confidence="0.50",
                    signals=signal,
                ),
            ),
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_FORMAT_CONFLICT"


@pytest.mark.anyio
async def test_declared_mime_and_extension_conflicts_are_warnings(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(
            _signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),
            _signal(
                ProbeSignalKind.DECLARED_MEDIA_TYPE,
                ProbeSignalOutcome.MISMATCH,
            ),
            _signal(ProbeSignalKind.EXTENSION, ProbeSignalOutcome.MISMATCH),
        ),
    )
    registry = ParserRegistry()
    parser = FakeParser(
        adapter_id="parser.csv",
        version="1.0.0",
        priority=1,
        probe_result=result,
    )
    registry.register(parser)

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert set(selected.probe_result.warnings) >= {
        "PARSER_DECLARED_MIME_MISMATCH",
        "PARSER_EXTENSION_MISMATCH",
    }


@pytest.mark.anyio
async def test_equivalent_declared_mime_case_and_parameters_are_not_conflict(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    declared = source.model_copy(update={"media_type": "Text/CSV; charset=UTF-8"})
    result = _result(
        declared,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(
            _signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),
            _signal(
                ProbeSignalKind.DECLARED_MEDIA_TYPE,
                ProbeSignalOutcome.MATCH,
            ),
        ),
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_result=result,
        )
    )

    async with registry.session() as session:
        selected = await session.select(declared, probe_context)

    assert "PARSER_DECLARED_MIME_MISMATCH" not in selected.probe_result.warnings


@pytest.mark.anyio
async def test_detected_mime_mismatch_adds_warning_without_declared_signal(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            probe_result=_result(
                source,
                adapter_id="parser.csv",
                format_id="csv",
                confidence="0.90",
                signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
            ),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.probe_result.warnings == ("PARSER_DECLARED_MIME_MISMATCH",)


@pytest.mark.anyio
async def test_supported_result_without_content_evidence_is_invalid(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_result=_result(
                source,
                adapter_id="parser.csv",
                format_id="csv",
                confidence="0.90",
                signals=(
                    _signal(
                        ProbeSignalKind.EXTENSION,
                        ProbeSignalOutcome.MATCH,
                    ),
                ),
            ),
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_PROBE_INVALID"


@pytest.mark.anyio
async def test_spoofed_extension_cannot_beat_content_confirmed_parser(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    metadata_only = ProbeResult(
        source=source.ref,
        adapter_id="parser.extension",
        adapter_version="1.0.0",
        supported=False,
        confidence=Decimal("0"),
        format_id="csv",
        signals=(_signal(ProbeSignalKind.EXTENSION, ProbeSignalOutcome.MATCH),),
    )
    content_result = _result(
        source,
        adapter_id="parser.signature",
        format_id="tsv",
        confidence="0.20",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    registry = ParserRegistry()
    registry.register_many(
        (
            FakeParser(
                adapter_id="parser.extension",
                priority=2**31 - 1,
                probe_result=metadata_only,
            ),
            FakeParser(
                adapter_id="parser.signature",
                priority=-(2**31),
                probe_result=content_result,
            ),
        )
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)

    assert selected.adapter_id == "parser.signature"


@pytest.mark.anyio
async def test_declared_mime_and_extension_only_remain_unsupported(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    metadata_only = ProbeResult(
        source=source.ref,
        adapter_id="parser.metadata",
        adapter_version="1.0.0",
        supported=False,
        confidence=Decimal("0"),
        format_id="csv",
        signals=(
            _signal(
                ProbeSignalKind.DECLARED_MEDIA_TYPE,
                ProbeSignalOutcome.MATCH,
            ),
            _signal(ProbeSignalKind.EXTENSION, ProbeSignalOutcome.MATCH),
        ),
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(adapter_id="parser.metadata", probe_result=metadata_only)
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_UNSUPPORTED_FORMAT"


@pytest.mark.anyio
async def test_forged_probe_result_is_revalidated_before_field_access(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    valid = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    forged = valid.model_copy(update={"confidence": "not-a-decimal"})
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_result=forged,
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_PROBE_INVALID"
    assert raised.value.details["reason"] == "result_schema"


@pytest.mark.anyio
async def test_forged_probe_schema_does_not_retain_raw_exception(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    valid = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    forged = valid.model_copy(
        update={"signals": cast(tuple[ProbeSignal, ...], (_HostileProbeValue(),))}
    )
    registry = ParserRegistry()
    registry.register(FakeParser(adapter_id="parser.csv", probe_result=forged))

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    rendered = " ".join(
        (
            repr(raised.value),
            repr(raised.value.details),
            repr(raised.value.__context__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
    assert raised.value.error_code == "PARSER_PROBE_INVALID"
    assert raised.value.details["reason"] == "result_schema"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "DO_NOT_LEAK_PROBE_SCHEMA" not in rendered


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("forgery", "reason"),
    (
        ("source", "source_identity"),
        ("adapter_id", "parser_identity"),
        ("version", "parser_identity"),
        ("score", "result_schema"),
        ("contradictory_signals", "supported_evidence"),
        ("unsupported_score", "unsupported_evidence"),
        ("unsupported_match", "unsupported_evidence"),
    ),
)
async def test_supported_candidate_does_not_mask_forged_probe_result(
    source: SourceArtifact,
    probe_context: ProbeContext,
    forgery: str,
    reason: str,
) -> None:
    valid = FakeParser(
        adapter_id="parser.alpha",
        probe_result=_result(
            source,
            adapter_id="parser.alpha",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        ),
    )
    forged = _result(
        source,
        adapter_id="parser.zeta",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    if forgery == "source":
        foreign_source = source.model_copy(update={"artifact_id": "source-foreign"})
        forged = forged.model_copy(update={"source": foreign_source.ref})
    elif forgery == "adapter_id":
        forged = forged.model_copy(update={"adapter_id": "parser.other"})
    elif forgery == "version":
        forged = forged.model_copy(update={"adapter_version": "2.0.0"})
    elif forgery == "score":
        forged = forged.model_copy(update={"confidence": Decimal("2")})
    elif forgery == "contradictory_signals":
        forged = forged.model_copy(
            update={
                "signals": (
                    _signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),
                    _signal(
                        ProbeSignalKind.CONTENT_MEDIA_TYPE,
                        ProbeSignalOutcome.MISMATCH,
                    ),
                )
            }
        )
    elif forgery == "unsupported_score":
        forged = forged.model_copy(
            update={
                "supported": False,
                "confidence": Decimal("0.10"),
                "signals": (),
            }
        )
    else:
        forged = forged.model_copy(
            update={
                "supported": False,
                "confidence": Decimal("0"),
            }
        )
    invalid = FakeParser(adapter_id="parser.zeta", probe_result=forged)
    registry = ParserRegistry()
    registry.register_many((invalid, valid))

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_PROBE_INVALID"
    assert raised.value.details["reason"] == reason
    assert valid.probe_call_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure_kind", "reason", "cause"),
    (
        ("call", "call_failed", "RuntimeError"),
        ("await", "probe_exception", "RuntimeError"),
        ("typed", "typed_probe_failure", "ParserError"),
    ),
)
async def test_unexpected_probe_failure_is_typed_and_redacted_without_fallback(
    source: SourceArtifact,
    probe_context: ProbeContext,
    failure_kind: str,
    reason: str,
    cause: str,
) -> None:
    marker = "DO_NOT_LEAK_PROBE_FAILURE"
    fallback = FakeParser(
        adapter_id="parser.zeta",
        probe_result=_result(
            source,
            adapter_id="parser.zeta",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        ),
    )
    if failure_kind == "call":
        failing = cast(
            Parser,
            _SynchronousProbeFailureParser(RuntimeError(marker)),
        )
    elif failure_kind == "await":
        failing = FakeParser(
            adapter_id="parser.alpha",
            probe_error=RuntimeError(marker),
        )
    else:
        failing = FakeParser(
            adapter_id="parser.alpha",
            probe_error=ParserError(
                error_code="PARSER_OUTPUT_INVALID",
                message=marker,
            ),
        )
    registry = ParserRegistry()
    registry.register_many((fallback, failing))

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    rendered = " ".join(
        (
            str(raised.value),
            repr(raised.value),
            repr(raised.value.details),
            repr(raised.value.__context__),
            repr(raised.value.__cause__),
            "".join(traceback.format_exception(raised.value)),
        )
    )
    assert raised.value.error_code == "PARSER_PROBE_FAILED"
    assert raised.value.details["reason"] == reason
    assert raised.value.cause == cause
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert marker not in rendered
    assert fallback.probe_call_count == 0


@pytest.mark.anyio
async def test_probe_cancellation_propagates_without_fallback(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def blocked_probe(
        probed_source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del context
        started.set()
        await blocked.wait()
        return _result(
            probed_source,
            adapter_id="parser.alpha",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        )

    first = FakeParser(adapter_id="parser.alpha", probe_factory=blocked_probe)
    fallback = FakeParser(
        adapter_id="parser.zeta",
        probe_result=_result(
            source,
            adapter_id="parser.zeta",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        ),
    )
    registry = ParserRegistry()
    registry.register_many((fallback, first))

    async with registry.session() as session:
        selection = asyncio.create_task(session.select(source, probe_context))
        await started.wait()
        selection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await selection

    assert fallback.probe_call_count == 0


@pytest.mark.anyio
async def test_probe_cancellation_drops_untrusted_context_and_notes(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    marker = "password=DO_NOT_LEAK_PROBE_CANCELLATION"
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.alpha",
            probe_error=_cancellation_with_context(marker),
        )
    )

    async with registry.session() as session:
        with pytest.raises(asyncio.CancelledError) as raised:
            await session.select(source, probe_context)

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.args == ()
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in "".join(traceback.format_exception(raised.value))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("synchronous", "reason"),
    ((False, "probe_exception"), (True, "call_failed")),
)
async def test_probe_base_exception_group_is_sanitized(
    source: SourceArtifact,
    probe_context: ProbeContext,
    *,
    synchronous: bool,
    reason: str,
) -> None:
    marker = "password=DO_NOT_LEAK_PROBE_GROUP"
    group = _hostile_base_exception_group(marker)
    parser: Parser
    if synchronous:
        parser = cast(Parser, _SynchronousProbeFailureParser(group))
    else:
        parser = FakeParser(adapter_id="parser.alpha", probe_error=group)
    registry = ParserRegistry()
    registry.register(parser)

    async with registry.session() as session:
        with pytest.raises(BaseExceptionGroup) as raised:
            await session.select(source, probe_context)

    cancellations = tuple(
        error
        for error in raised.value.exceptions
        if isinstance(error, asyncio.CancelledError)
    )
    probe_errors = tuple(
        error for error in raised.value.exceptions if isinstance(error, ParserError)
    )
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert len(cancellations) == 1
    assert cancellations[0].args == ()
    assert cancellations[0].__context__ is None
    assert cancellations[0].__cause__ is None
    assert getattr(cancellations[0], "__notes__", ()) == ()
    assert len(probe_errors) == 1
    assert probe_errors[0].error_code == "PARSER_PROBE_FAILED"
    assert probe_errors[0].details["reason"] == reason
    assert marker not in "".join(traceback.format_exception(raised.value))


@pytest.mark.anyio
async def test_selection_lock_cancellation_drops_untrusted_payload(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe(
        probed_source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del context
        started.set()
        await release.wait()
        return _result(
            probed_source,
            adapter_id="parser.alpha",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        )

    registry = ParserRegistry()
    registry.register(
        FakeParser(adapter_id="parser.alpha", probe_factory=blocked_probe)
    )

    async with registry.session() as session:
        first_selection = asyncio.create_task(session.select(source, probe_context))
        await started.wait()
        waiting_selection = asyncio.create_task(session.select(source, probe_context))
        await asyncio.sleep(0)
        marker = "password=DO_NOT_LEAK_SELECTION_LOCK_CANCELLATION"
        waiting_selection.cancel(marker)

        with pytest.raises(asyncio.CancelledError) as raised:
            await waiting_selection
        assert type(raised.value) is asyncio.CancelledError
        assert raised.value.args == ()
        assert raised.value.__context__ is None
        assert raised.value.__cause__ is None
        assert getattr(raised.value, "__notes__", ()) == ()
        assert marker not in "".join(traceback.format_exception(raised.value))

        release.set()
        assert (await first_selection).adapter_id == "parser.alpha"


@pytest.mark.anyio
async def test_probe_security_error_is_preserved(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    expected = _security_error_with_context(
        "password=DO_NOT_LEAK_PROBE_SECURITY_CONTEXT"
    )
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_error=expected,
        )
    )

    async with registry.session() as session:
        with pytest.raises(SecurityPolicyError) as raised:
            await session.select(source, probe_context)

    assert raised.value is expected
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert "DO_NOT_LEAK_PROBE_SECURITY_CONTEXT" not in "".join(
        traceback.format_exception(raised.value)
    )


@pytest.mark.anyio
async def test_sync_probe_security_error_is_preserved_without_raw_chain(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    marker = "password=DO_NOT_LEAK_SYNC_PROBE_SECURITY_CONTEXT"
    expected = _security_error_with_context(marker)
    registry = ParserRegistry()
    registry.register(cast(Parser, _SynchronousProbeFailureParser(expected)))

    async with registry.session() as session:
        with pytest.raises(SecurityPolicyError) as raised:
            await session.select(source, probe_context)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value is expected
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert getattr(raised.value, "__notes__", ()) == ()
    assert marker not in rendered


@pytest.mark.anyio
async def test_detected_media_type_must_have_mime_syntax(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    ).model_copy(update={"detected_media_type": "not a mime"})
    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_result=result,
        )
    )

    async with registry.session() as session:
        with pytest.raises(ParserError) as raised:
            await session.select(source, probe_context)

    assert raised.value.error_code == "PARSER_PROBE_INVALID"
    assert raised.value.details["reason"] == "detected_media_type"


@pytest.mark.anyio
async def test_session_exit_waits_for_in_flight_selection(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe(
        probed_source: SourceArtifact,
        context: ProbeContext,
    ) -> ProbeResult:
        del context
        started.set()
        await release.wait()
        return _result(
            probed_source,
            adapter_id="parser.csv",
            format_id="csv",
            confidence="0.90",
            signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
        )

    registry = ParserRegistry()
    registry.register(
        FakeParser(
            adapter_id="parser.csv",
            version="1.0.0",
            priority=1,
            probe_factory=blocked_probe,
        )
    )
    session = registry.session()
    await session.__aenter__()
    selection = asyncio.create_task(session.select(source, probe_context))
    await started.wait()

    exit_started = asyncio.Event()

    async def exit_session() -> None:
        exit_started.set()
        await session.__aexit__(None, None, None)

    exit_task = asyncio.create_task(exit_session())
    await exit_started.wait()
    with pytest.raises(ParserError) as frozen:
        registry.register(
            FakeParser(adapter_id="parser.second", version="1.0.0", priority=0)
        )
    release.set()

    with pytest.raises(ParserError) as closed:
        await selection
    await exit_task
    registry.register(
        FakeParser(adapter_id="parser.second", version="1.0.0", priority=0)
    )

    assert frozen.value.error_code == "PARSER_REGISTRY_FROZEN"
    assert closed.value.error_code == "PARSER_SESSION_CLOSED"


@pytest.mark.anyio
async def test_selected_handle_exposes_read_only_probe_result(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    registry = ParserRegistry()
    parser = FakeParser(
        adapter_id="parser.csv",
        version="1.0.0",
        priority=1,
        probe_result=result,
    )
    registry.register(parser)

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(AttributeError):
            selected.probe_result = result  # type: ignore[misc]


@pytest.mark.anyio
async def test_selected_handle_cannot_parse_another_artifact_with_same_hash(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    registry = ParserRegistry()
    parser = FakeParser(
        adapter_id="parser.csv",
        version="1.0.0",
        priority=1,
        probe_result=result,
    )
    registry.register(parser)
    other_source = source.model_copy(update={"artifact_id": "source-2"})
    parse_context = ParseContext(
        reader=probe_context.reader,
        source_fingerprint=source.source_fingerprint,
        max_bytes=32,
        max_records=10,
        max_nesting_depth=4,
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(ParserError) as raised:
            selected.parse(other_source, parse_context)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "selected_source_identity"
    assert parser.parse_call_count == 0


@pytest.mark.anyio
async def test_selected_handle_rejects_changed_source_metadata_with_same_ref(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = _result(
        source,
        adapter_id="parser.csv",
        format_id="csv",
        confidence="0.90",
        signals=(_signal(ProbeSignalKind.SIGNATURE, ProbeSignalOutcome.MATCH),),
    )
    parser = FakeParser(
        adapter_id="parser.csv",
        version="1.0.0",
        priority=1,
        probe_result=result,
    )
    registry = ParserRegistry()
    registry.register(parser)
    changed_source = source.model_copy(
        update={
            "display_name": "renamed.json",
            "media_type": "application/json",
            "size_bytes": source.size_bytes + 1,
        }
    )
    parse_context = ParseContext(
        reader=probe_context.reader,
        source_fingerprint=source.source_fingerprint,
        max_bytes=32,
        max_records=10,
        max_nesting_depth=4,
    )

    assert changed_source.ref == source.ref
    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        with pytest.raises(ParserError) as raised:
            selected.parse(changed_source, parse_context)

    assert raised.value.error_code == "PARSER_OUTPUT_INVALID"
    assert raised.value.details["reason"] == "selected_source_identity"
    assert parser.parse_call_count == 0


@pytest.mark.anyio
async def test_selected_handle_passes_probe_encoding_to_parser_context(
    source: SourceArtifact,
    probe_context: ProbeContext,
) -> None:
    result = ProbeResult(
        source=source.ref,
        adapter_id="fake.parser",
        adapter_version="1.0.0",
        supported=True,
        confidence=Decimal("0.90"),
        detected_media_type="text/plain",
        detected_encoding="cp1251",
        format_id="txt",
        signals=(
            _signal(
                ProbeSignalKind.INTERNAL_STRUCTURE,
                ProbeSignalOutcome.MATCH,
            ),
        ),
    )
    parser = _ContextRecordingParser(probe_result=result)
    registry = ParserRegistry()
    registry.register(parser)
    parse_context = ParseContext(
        reader=probe_context.reader,
        source_fingerprint=source.source_fingerprint,
        max_bytes=32,
        max_records=10,
        max_nesting_depth=4,
    )

    async with registry.session() as session:
        selected = await session.select(source, probe_context)
        stream = selected.parse(source, parse_context)

        assert parse_context.detected_encoding is None
        assert parser.parse_context is not None
        assert parser.parse_context.detected_encoding == "cp1251"
        await stream.aclose()
