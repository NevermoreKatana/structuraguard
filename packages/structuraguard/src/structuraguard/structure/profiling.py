"""Async StructuralProfiler: конечные budgets, никакого external I/O."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import BuiltInErrorCode, ProducerMetadata
from structuraguard.contracts.parsing import StructureProfile
from structuraguard.contracts.source import ExtractedBatch, ExtractedDatasetManifest
from structuraguard.contracts.structure import (
    ProfileCoverage,
    StructuralProfilingOptions,
)
from structuraguard.exceptions import (
    ParserError,
    SecurityPolicyError,
    StructuraGuardError,
    StructuralProfilingError,
)
from structuraguard.structure._observations import Observations
from structuraguard.structure._samples import Samples
from structuraguard.structure._stream import StreamCheck, invalid, limit
from structuraguard.structure.document import analyze_documents
from structuraguard.structure.tabular import analyze_tables
from structuraguard.structure.text import analyze_text
from structuraguard.structure.tree import analyze_trees


@runtime_checkable
class _Closable(Protocol):
    async def aclose(self) -> None: ...


async def _single(batch: ExtractedBatch) -> AsyncIterator[ExtractedBatch]:
    yield batch


def _source_failure(error: Exception, reason: str) -> StructuraGuardError:
    code = BuiltInErrorCode.STRUCTURE_INPUT_INVALID
    if isinstance(error, StructuraGuardError) and error.error_code in BuiltInErrorCode:
        code = BuiltInErrorCode(error.error_code)
    error_type: type[StructuraGuardError] = StructuralProfilingError
    if isinstance(error, SecurityPolicyError):
        error_type = SecurityPolicyError
    elif isinstance(error, ParserError):
        error_type = ParserError
    # Даже typed adapter error может содержать raw values в message/details.
    # Сохраняется только известный code, без исходного exception context.
    return error_type(
        error_code=code.value,
        message="Ошибка physical source при structural profiling",
        details={"reason": reason},
    )


def _source_iterator(
    batches: ExtractedBatch | AsyncIterable[ExtractedBatch],
) -> AsyncIterator[ExtractedBatch]:
    try:
        return aiter(
            _single(batches) if isinstance(batches, ExtractedBatch) else batches
        )
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise _source_failure(error, "source_open_failed") from None


async def _next_batch(iterator: AsyncIterator[ExtractedBatch]) -> ExtractedBatch:
    try:
        return await anext(iterator)
    except StopAsyncIteration:
        raise
    except BaseException as error:
        # Только external iterator call; cancellation/process-control не подавляются.
        if not isinstance(error, Exception):
            raise
        raise _source_failure(error, "source_read_failed") from None


@dataclass(frozen=True, slots=True)
class ProfiledSource:
    """Внутренний bounded snapshot для analysis; не сохраняет исходные batches."""

    profile: StructureProfile
    manifest: ExtractedDatasetManifest
    samples: Samples


class StructuralProfiler:
    """Получить fingerprint-bound профиль physical batches без LLM/сети.

    Args:
        options: Immutable бюджеты sample, input stream и profile output.

    Raises:
        ValueError: Неверный тип options или значения за пределами hard caps.

    ``profile`` принимает один terminal batch или AsyncIterable полного
    extraction. Профиль описывает гипотезы и coverage, не разрешает execution.
    Instance не сохраняет source, samples или изменяемый state между вызовами.
    """

    def __init__(self, *, options: StructuralProfilingOptions | None = None) -> None:
        if options is not None and type(options) is not StructuralProfilingOptions:
            raise ValueError("options должен быть StructuralProfilingOptions")
        self._options = StructuralProfilingOptions.model_validate(
            (options or StructuralProfilingOptions()).model_dump(mode="python")
        )

    @property
    def options(self) -> StructuralProfilingOptions:
        """Вернуть immutable options, действующие для каждого независимого run."""
        return self._options

    async def profile(
        self, batches: ExtractedBatch | AsyncIterable[ExtractedBatch]
    ) -> StructureProfile:
        """Прочитать, проверить и закрыть stream; вернуть bounded StructureProfile.

        Args:
            batches: Один terminal batch либо полный одноразовый async stream
                одного extraction, включая terminal manifest.

        Returns:
            Профиль schema 1.1.0 с coverage, evidence и кандидатами. Confidence
            является эвристическим score; semantic meaning не назначается.

        Raises:
            StructuralProfilingError: Нарушена lineage, отсутствует terminal
                manifest, source iterator завершился ошибкой, превышен deadline
                либо не завершён continuation.
            ParserError: Безопасная копия ошибки technical parser.
            SecurityPolicyError: Превышен operational resource limit либо
                technical parser отклонил источник по security policy.

        Пустой source возвращает schema 1.1.0 с пустым evidence и coverage.
        Sampling overflow не маскируется: отражается в coverage.reasons.
        Cancellation распространяется после закрытия iterator с close-hook.
        Source exceptions очищаются: сохраняются только известные codes и
        категории parser/security, без raw message/details. Сам профиль может
        содержать sensitive labels/values; безопасным журналом он не является.
        """
        return (await self._inspect(batches)).profile

    async def _inspect(
        self, batches: ExtractedBatch | AsyncIterable[ExtractedBatch]
    ) -> ProfiledSource:
        """Внутренний проход для analyzer с теми же budgets и cleanup contract."""
        iterator = _source_iterator(batches)
        primary: BaseException | None = None
        try:
            async with asyncio.timeout(self.options.max_processing_seconds):
                return await self._profile(iterator)
        except TimeoutError:
            primary = StructuralProfilingError(
                error_code="PROCESSING_TIMEOUT",
                message="Истёк deadline structural profiling",
            )
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            if isinstance(iterator, _Closable):
                try:
                    async with asyncio.timeout(1):
                        await iterator.aclose()
                except asyncio.CancelledError:
                    raise
                except BaseException as cleanup:
                    if not isinstance(cleanup, Exception):
                        raise
                    if primary is None:
                        raise invalid("iterator_cleanup") from None
                    primary.add_note("STRUCTURE_INPUT_INVALID: iterator_cleanup")

    async def _profile(self, iterator: AsyncIterator[ExtractedBatch]) -> ProfiledSource:
        check = StreamCheck(self.options)
        samples = Samples(self.options)
        while True:
            try:
                batch = await _next_batch(iterator)
            except StopAsyncIteration:
                break
            checked = check.accept(batch)
            samples.consume(checked, check.known_refs)
            # Bounded batch work между checkpoints не оставляет timer consumer.
            await asyncio.sleep(0)
        manifest = check.finish()
        samples.bind(set(manifest.source_index.refs))
        if samples.retained == samples.seen:
            samples.reasons.difference_update({"sample_items", "sample_bytes"})
        output = Observations(samples, manifest, self.options)
        for analyze in (analyze_tables, analyze_trees, analyze_text, analyze_documents):
            analyze(output)
            await asyncio.sleep(0)
        candidates = output.ranked()
        coverage = ProfileCoverage(
            options_fingerprint=canonical_sha256_value(self.options),
            seen_items=samples.seen,
            sampled_items=samples.retained,
            sampled_bytes=samples.bytes,
            skipped_items=samples.seen - samples.retained,
            complete=samples.seen == samples.retained and not samples.reasons,
            reasons=tuple(sorted(samples.reasons)),
        )
        evidence = tuple(
            dict.fromkeys(
                ref
                for observation in output.evidence
                for ref in observation.source_refs
            )
        )
        profile = StructureProfile(
            profile_id="profile_"
            + canonical_sha256_value(
                (manifest.extraction_fingerprint, coverage.options_fingerprint)
            )[-24:],
            schema_version="1.1.0",
            source=manifest.source,
            extraction_fingerprint=manifest.extraction_fingerprint,
            profile_fingerprint="sha256:" + "0" * 64,
            producer=ProducerMetadata(
                component_id="structural_profiler",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            evidence=evidence,
            observations=tuple(output.evidence),
            candidates=candidates,
            coverage=coverage,
        )
        if (
            len(profile.canonical_json().encode("utf-8"))
            > self.options.max_profile_bytes
        ):
            raise limit("profile_output_bytes", self.options.max_profile_bytes)
        return ProfiledSource(profile, manifest, samples)
