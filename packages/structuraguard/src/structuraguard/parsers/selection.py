"""Детерминированный выбор trusted technical parser."""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import partial

from structuraguard.contracts.source import (
    ProbeResult,
    ProbeSignalKind,
    ProbeSignalOutcome,
    SourceArtifact,
)
from structuraguard.exceptions import ParserError, SecurityPolicyError
from structuraguard.ports.source import ProbeContext

from ._registration import RegisteredParser
from .execution import (
    _BoundaryError,
    _normalize_boundary_group,
    _raise_boundary_outcomes,
    _sanitize_cancelled_error,
)

_STRONG_SIGNAL_KINDS = frozenset(
    {
        ProbeSignalKind.INTERNAL_STRUCTURE,
        ProbeSignalKind.SIGNATURE,
        ProbeSignalKind.CONTENT_MEDIA_TYPE,
    }
)
_MEDIA_TYPE_PATTERN = re.compile(
    r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+/[!#$%&'*+.^_`|~0-9A-Za-z-]+$"
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ParserSelection:
    """Проверенный результат выбора parser из одного registry snapshot."""

    registration: RegisteredParser
    probe_result: ProbeResult


@dataclass(frozen=True, slots=True, kw_only=True)
class _EligibleCandidate:
    registration: RegisteredParser
    result: ProbeResult
    evidence: tuple[bool, bool, bool]


def _probe_error(
    code: str,
    adapter_id: str,
    reason: str,
    *,
    cause: BaseException | None = None,
) -> ParserError:
    return ParserError(
        error_code=code,
        message="Parser вернул недопустимый результат probe.",
        details={"adapter_id": adapter_id, "reason": reason},
        cause=cause,
    )


def _detach_security_error(error: SecurityPolicyError) -> SecurityPolicyError:
    """Удалить чужую exception chain перед сохранением typed security outcome."""

    error.__context__ = None
    error.__cause__ = None
    error.__traceback__ = None
    if hasattr(error, "__notes__"):
        del error.__notes__
    return error


def _probe_group_failure(
    adapter_id: str,
    reason: str,
    error: BaseException,
) -> ParserError | SecurityPolicyError:
    if isinstance(error, SecurityPolicyError):
        return _detach_security_error(error)
    return _probe_error(
        "PARSER_PROBE_FAILED",
        adapter_id,
        reason,
        cause=error,
    )


def _validate_probe_result(
    registration: RegisteredParser,
    source: SourceArtifact,
    result: object,
) -> tuple[ProbeResult, tuple[bool, bool, bool]]:
    identity = registration.identity
    if type(result) is not ProbeResult:
        raise _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "result_type",
        )
    validation_failure: ParserError | None = None
    try:
        payload = result.model_dump(
            mode="python",
            round_trip=True,
            warnings="error",
        )
        result = ProbeResult.model_validate(payload, strict=True)
    except Exception as error:  # adapter DTO является недоверенной boundary
        validation_failure = _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "result_schema",
            cause=error,
        )
    if validation_failure is not None:
        raise validation_failure from None
    if result.source != source.ref:
        raise _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "source_identity",
        )
    if (
        result.adapter_id != identity.adapter_id
        or result.adapter_version != identity.version
    ):
        raise _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "parser_identity",
        )
    if (
        result.detected_media_type is not None
        and _MEDIA_TYPE_PATTERN.fullmatch(result.detected_media_type) is None
    ):
        raise _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "detected_media_type",
        )

    outcomes = {signal.kind: signal.outcome for signal in result.signals}
    strong_matches = {
        kind
        for kind in _STRONG_SIGNAL_KINDS
        if outcomes.get(kind) is ProbeSignalOutcome.MATCH
    }
    strong_mismatches = {
        kind
        for kind in _STRONG_SIGNAL_KINDS
        if outcomes.get(kind) is ProbeSignalOutcome.MISMATCH
    }
    if result.supported:
        if (
            result.confidence <= Decimal(0)
            or result.format_id is None
            or not strong_matches
            or strong_mismatches
        ):
            raise _probe_error(
                "PARSER_PROBE_INVALID",
                identity.adapter_id,
                "supported_evidence",
            )
    elif result.confidence != Decimal(0) or strong_matches:
        raise _probe_error(
            "PARSER_PROBE_INVALID",
            identity.adapter_id,
            "unsupported_evidence",
        )

    warnings = set(result.warnings)
    declared_media_type = source.media_type.partition(";")[0].strip().casefold()
    if outcomes.get(
        ProbeSignalKind.DECLARED_MEDIA_TYPE
    ) is ProbeSignalOutcome.MISMATCH or (
        result.supported
        and result.detected_media_type is not None
        and declared_media_type != result.detected_media_type.casefold()
    ):
        warnings.add("PARSER_DECLARED_MIME_MISMATCH")
    if outcomes.get(ProbeSignalKind.EXTENSION) is ProbeSignalOutcome.MISMATCH:
        warnings.add("PARSER_EXTENSION_MISMATCH")
    if tuple(sorted(warnings)) != result.warnings:
        result = result.model_copy(update={"warnings": tuple(sorted(warnings))})

    evidence = (
        ProbeSignalKind.INTERNAL_STRUCTURE in strong_matches,
        ProbeSignalKind.SIGNATURE in strong_matches,
        ProbeSignalKind.CONTENT_MEDIA_TYPE in strong_matches,
    )
    return result, evidence


async def select_parser(
    registrations: tuple[RegisteredParser, ...],
    source: SourceArtifact,
    context: ProbeContext,
) -> ParserSelection:
    """Последовательно probe parsers и выбрать воспроизводимого победителя."""

    if context.source_fingerprint != source.source_fingerprint:
        raise ParserError(
            error_code="PARSER_PROBE_INVALID",
            message="Probe context относится к другому source snapshot.",
            details={"reason": "context_source_identity"},
        )
    if not registrations:
        raise ParserError(
            error_code="PARSER_NOT_FOUND",
            message="В registry нет trusted parsers.",
        )

    eligible: list[_EligibleCandidate] = []
    unavailable_count = 0
    for registration in registrations:
        identity = registration.identity
        call_cancellation: asyncio.CancelledError | None = None
        call_grouped_failure: tuple[_BoundaryError, ...] = ()
        call_failure: ParserError | SecurityPolicyError | None = None
        try:
            probe_call = registration.parser.probe(source, context)
        except BaseExceptionGroup as error:
            if isinstance(error, ExceptionGroup):
                call_failure = _probe_error(
                    "PARSER_PROBE_FAILED",
                    identity.adapter_id,
                    "call_failed",
                    cause=error,
                )
            else:
                call_grouped_failure = _normalize_boundary_group(
                    error,
                    partial(
                        _probe_group_failure,
                        identity.adapter_id,
                        "call_failed",
                    ),
                )
        except asyncio.CancelledError as error:
            call_cancellation = _sanitize_cancelled_error(error)
        except SecurityPolicyError as error:
            call_failure = _detach_security_error(error)
        except Exception as error:
            call_failure = _probe_error(
                "PARSER_PROBE_FAILED",
                identity.adapter_id,
                "call_failed",
                cause=error,
            )
        if call_cancellation is not None:
            raise call_cancellation from None
        if call_grouped_failure:
            _raise_boundary_outcomes(
                "Parser probe call завершился с несколькими ошибками.",
                call_grouped_failure,
            )
        if call_failure is not None:
            raise call_failure from None
        if not inspect.isawaitable(probe_call):
            raise _probe_error(
                "PARSER_PROBE_INVALID",
                identity.adapter_id,
                "probe_not_awaitable",
            )
        probe_cancellation: asyncio.CancelledError | None = None
        probe_grouped_failure: tuple[_BoundaryError, ...] = ()
        probe_failure: ParserError | SecurityPolicyError | None = None
        try:
            result = await probe_call
        except BaseExceptionGroup as error:
            if isinstance(error, ExceptionGroup):
                probe_failure = _probe_error(
                    "PARSER_PROBE_FAILED",
                    identity.adapter_id,
                    "probe_exception",
                    cause=error,
                )
            else:
                probe_grouped_failure = _normalize_boundary_group(
                    error,
                    partial(
                        _probe_group_failure,
                        identity.adapter_id,
                        "probe_exception",
                    ),
                )
        except asyncio.CancelledError as error:
            probe_cancellation = _sanitize_cancelled_error(error)
        except SecurityPolicyError as error:
            probe_failure = _detach_security_error(error)
        except ParserError as error:
            if error.error_code == "PARSER_DEPENDENCY_UNAVAILABLE":
                unavailable_count += 1
                continue
            probe_failure = _probe_error(
                "PARSER_PROBE_FAILED",
                identity.adapter_id,
                "typed_probe_failure",
                cause=error,
            )
        except Exception as error:
            probe_failure = _probe_error(
                "PARSER_PROBE_FAILED",
                identity.adapter_id,
                "probe_exception",
                cause=error,
            )
        if probe_cancellation is not None:
            raise probe_cancellation from None
        if probe_grouped_failure:
            _raise_boundary_outcomes(
                "Parser probe завершился с несколькими ошибками.",
                probe_grouped_failure,
            )
        if probe_failure is not None:
            raise probe_failure from None

        validated, evidence = _validate_probe_result(registration, source, result)
        if validated.supported:
            eligible.append(
                _EligibleCandidate(
                    registration=registration,
                    result=validated,
                    evidence=evidence,
                )
            )

    if not eligible:
        if unavailable_count:
            raise ParserError(
                error_code="PARSER_DEPENDENCY_UNAVAILABLE",
                message="Подходящий parser может требовать недоступную dependency.",
                details={"unavailable_count": unavailable_count},
                retryable=True,
            )
        raise ParserError(
            error_code="PARSER_UNSUPPORTED_FORMAT",
            message="Ни один parser не подтвердил формат по содержимому.",
        )

    maximal_evidence = max(candidate.evidence for candidate in eligible)
    strongest = tuple(
        candidate for candidate in eligible if candidate.evidence == maximal_evidence
    )
    format_ids = {candidate.result.format_id for candidate in strongest}
    if len(format_ids) != 1:
        raise ParserError(
            error_code="PARSER_FORMAT_CONFLICT",
            message="Сильные content signals неоднозначно определяют формат.",
            details={"candidate_count": len(strongest)},
        )

    winner = min(
        strongest,
        key=lambda candidate: (
            -candidate.result.confidence,
            -candidate.registration.identity.priority,
            candidate.registration.identity.adapter_id,
        ),
    )
    return ParserSelection(
        registration=winner.registration,
        probe_result=winner.result,
    )
