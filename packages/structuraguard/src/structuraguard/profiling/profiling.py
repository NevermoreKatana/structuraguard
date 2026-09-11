"""Async normalized profiler: terminal-only result, bounded state и safe summary."""

import asyncio
from collections import Counter
from collections.abc import AsyncIterable, AsyncIterator, Callable
from time import monotonic
from typing import Protocol, runtime_checkable

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import DataClassification, ProducerMetadata
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    SemanticFieldRef,
)
from structuraguard.contracts.profiling import (
    ExamplePolicy,
    NormalizedDataProfile,
    NormalizedFieldProfile,
    NormalizedProfileContext,
    NormalizedProfilingOptions,
    PIIClassificationRequest,
    PIIClassificationResult,
    ProfileExample,
)
from structuraguard.domain.normalized_fingerprint import NormalizedContentHasher
from structuraguard.ports.security import PIIClassifier
from structuraguard.profiling._bounded import ratio
from structuraguard.profiling._context import ContextEvidence
from structuraguard.profiling._statistics import FieldStats
from structuraguard.profiling._stream import (
    Ledger,
    StreamCheck,
    failure,
    limit,
    preflight,
)
from structuraguard.profiling.pii import (
    LocalPIIClassifier,
    classification_rank,
    maximum_classification,
)


@runtime_checkable
class _Closable(Protocol):
    async def aclose(self) -> None: ...


async def _single(batch: NormalizedBatch) -> AsyncIterator[NormalizedBatch]:
    yield batch


def _iterator(
    batches: NormalizedBatch | AsyncIterable[NormalizedBatch],
) -> AsyncIterator[NormalizedBatch]:
    try:
        return aiter(
            _single(batches) if isinstance(batches, NormalizedBatch) else batches
        )
    except BaseException as error:
        # Единственная внешняя boundary открытия; raw error message не переносится.
        if not isinstance(error, Exception):
            raise
        raise failure("source_open") from None


async def _next(iterator: AsyncIterator[NormalizedBatch]) -> NormalizedBatch:
    try:
        return await anext(iterator)
    except StopAsyncIteration:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise failure("source_read") from None


def _discard_close_error(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


async def _call_close(iterator: _Closable) -> None:
    # Некорректный adapter может бросить ошибку ещё при вызове aclose().
    # Выполняем и вызов, и await внутри одной очищаемой task boundary.
    await iterator.aclose()


async def _close(iterator: AsyncIterator[NormalizedBatch], seconds: float) -> None:
    if not isinstance(iterator, _Closable):
        return
    task = asyncio.create_task(_call_close(iterator))
    cancelled = False
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not task.done() and loop.time() < end:
        try:
            await asyncio.wait((task,), timeout=max(0, end - loop.time()))
        except asyncio.CancelledError:
            # Повторная отмена не бросает owned cleanup до отдельного deadline.
            cancelled = True
    if not task.done():
        task.cancel()
        task.add_done_callback(_discard_close_error)
        if cancelled:
            raise asyncio.CancelledError
        raise failure("cleanup_timeout", code="CLEANUP_FAILED")
    if cancelled:
        _discard_close_error(task)
        raise asyncio.CancelledError
    try:
        task.result()
    except BaseException as error:
        if not isinstance(error, Exception | asyncio.CancelledError):
            raise
        raise failure("cleanup_failed", code="CLEANUP_FAILED") from None


class NormalizedDataProfiler:
    """Собрать ограниченный профиль после семантического разбора.

    Args:
        options: Неизменяемые лимиты, правила locale и samples. None выбирает
            NormalizedProfilingOptions с masked examples и locale unspecified.
        classifier: Доверенный локальный PII adapter. По умолчанию используется
            LocalPIIClassifier; ответ адаптера проверяется по binding и минимуму
            классификации. Адаптер получает агрегаты, а не raw examples.
        timer: Доверенная монотонная функция времени в секундах для checkpoints.
            Общий async deadline дополнительно контролируется event loop.

    Raises:
        ValueError: Options нарушают контракт или совокупные бюджеты, в том числе
            hard cap 4096 для цифр коэффициента и exponent порогов inference.

    Конструктор не выполняет I/O. Состояние статистик создаётся для каждого вызова
    отдельно; параллельные вызовы также разделяют только переданные зависимости.
    Profiler не изменяет данные, не выполняет mapping/SQL/LLM и не выдаёт approval.
    Полный результат чувствителен: для logs используется только safe_summary().
    """

    def __init__(
        self,
        options: NormalizedProfilingOptions | None = None,
        *,
        classifier: PIIClassifier | None = None,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        supplied = options if options is not None else NormalizedProfilingOptions()
        self.options = NormalizedProfilingOptions.model_validate(
            supplied.model_dump(mode="python")
        )
        self.classifier = classifier if classifier is not None else LocalPIIClassifier()
        self.timer = timer

    async def profile(
        self,
        batches: NormalizedBatch | AsyncIterable[NormalizedBatch],
        *,
        context: NormalizedProfileContext | None = None,
    ) -> NormalizedDataProfile:
        """Прочитать завершённый normalized dataset одним проходом.

        Args:
            batches: Один terminal NormalizedBatch с index 0 либо полный async
                поток schema 1.1.0/1.2.0 от index 0 до terminal и EOF. Preview
                и последний batch отдельно от предшествующих не принимаются.
            context: Минимальный класс данных и необязательные labels с lineage.
                По умолчанию INTERNAL без labels. Класс исходного источника
                передаёт caller: из NormalizedBatch он не восстанавливается.

        Returns:
            Неизменяемый NormalizedDataProfile после проверки stream и cleanup.
            Пустой завершённый dataset допустим; schema-only поля сохраняются.
            Значения, schema и исходные batches не изменяются.

        Raises:
            NormalizedProfilingError: Нарушение stream/lineage, неподдерживаемая
                версия, исчерпание лимита, timeout, ошибка PII adapter или cleanup.
                error_code начинается с NORMALIZED_PROFILE_; details["reason"]
                содержит безопасную причину без текста исходного исключения.
            asyncio.CancelledError: Отмена вызова, включая повторную отмену cleanup.

        Полученный iterator потребляется и закрывается при успехе, ошибке и отмене;
        source/provider ресурсами владеет caller. Результат не возвращается до
        успешного cleanup. Дополнительная cleanup failure не заменяет первичную
        ошибку, а добавляет статическую note. Async deadline не изолирует процесс
        от исполняемого кода некооперативного source/classifier.
        Полный профиль может содержать PII в labels, extrema и локальных samples;
        для журналирования предназначен только result.safe_summary().
        """
        supplied = context if context is not None else NormalizedProfileContext()
        preflight(supplied, self.options, maximum=self.options.max_manifest_bytes)
        try:
            checked_context = NormalizedProfileContext.model_validate(
                supplied.model_dump(mode="python")
            )
        except (ValueError, TypeError, AttributeError):
            raise failure("context_contract") from None
        iterator = _iterator(batches)
        primary: BaseException | None = None
        try:
            async with asyncio.timeout(self.options.max_processing_seconds):
                result = await self._run(iterator, checked_context)
        except TimeoutError:
            primary = failure("deadline", code="TIMEOUT")
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await _close(iterator, self.options.cleanup_seconds)
            except BaseException as cleanup:
                if primary is None or isinstance(cleanup, asyncio.CancelledError):
                    raise
                primary.add_note("NORMALIZED_PROFILE_CLEANUP_FAILED")
        return result

    async def _run(
        self,
        iterator: AsyncIterator[NormalizedBatch],
        context: NormalizedProfileContext,
    ) -> NormalizedDataProfile:
        options = self.options
        end = self.timer() + options.max_processing_seconds
        ledger = Ledger(options.max_state_bytes)
        checker = StreamCheck(options, ledger)
        hasher = NormalizedContentHasher()
        evidence = ContextEvidence(context, options, ledger)
        fields: dict[SemanticFieldRef, FieldStats] = {}
        entities: Counter[str] = Counter()
        values = 0
        while True:
            self._deadline(end)
            try:
                batch = await _next(iterator)
            except StopAsyncIteration:
                break
            checked = checker.accept(batch)
            self._bind_context(context, checked)
            hasher.consume(checked)
            for record in checked.records:
                evidence.record(record)
                for entity in record.entities:
                    entities[entity.entity_type] += 1
                    for value in entity.values:
                        ref = SemanticFieldRef(
                            entity_type=entity.entity_type, field_name=value.field_name
                        )
                        if ref not in fields:
                            fields[ref] = FieldStats(
                                ref, value.semantic_type, options, ledger
                            )
                        fields[ref].add(value)
                        evidence.value(ref, value)
                        values += 1
                        if values % 256 == 0:
                            self._deadline(end)
                            await asyncio.sleep(0)
            del batch, checked
            await asyncio.sleep(0)
        manifest = checker.finish()
        for label in context.labels:
            if label.field not in checker.schema:
                raise failure("context_field")
        for ref, declared in checker.schema.items():
            if ref not in fields:
                fields[ref] = FieldStats(ref, declared, options, ledger)
        result_fields: list[NormalizedFieldProfile] = []
        output_bytes = 4096
        for ref in sorted(fields, key=lambda r: (r.entity_type, r.field_name)):
            self._deadline(end)
            stats = fields[ref]
            stats.reasons.update(evidence.reasons.get(ref, ()))
            labels = tuple(
                sorted(
                    evidence.labels.get(ref, ()),
                    key=lambda label: (label.kind, label.origin, label.text),
                )
            )
            request = PIIClassificationRequest(
                field=ref,
                labels=labels,
                patterns=stats.evidence(),
                checked_count=stats.checked,
                skipped_count=stats.skipped + int("context_limit" in stats.reasons),
                value_categories=tuple(sorted(stats.categories)),
                minimum_classification=context.data_classification,
            )
            pii = await self._classify(request)
            field = self._field(stats, entities[ref.entity_type], request, pii)
            size = len(field.canonical_json().encode())
            output_bytes += size + 1
            if output_bytes > options.max_profile_bytes:
                raise limit("profile_bytes")
            ledger.add(size * 4 + 2048)
            result_fields.append(field)
        self._deadline(end)
        relationships = evidence.output()
        output_bytes += sum(
            len(item.canonical_json().encode()) + 1 for item in relationships
        )
        if output_bytes > options.max_profile_bytes:
            raise limit("profile_bytes")
        ledger.add(
            sum(len(item.canonical_json().encode()) * 4 for item in relationships)
        )
        result = NormalizedDataProfile(
            producer=ProducerMetadata(
                component_id="normalized_profiler",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            source=manifest.source,
            extraction_fingerprint=manifest.extraction_fingerprint,
            parse_plan_fingerprint=manifest.parse_plan_fingerprint,
            normalized_manifest_fingerprint=manifest.normalized_fingerprint,
            normalized_data_fingerprint=hasher.finish(manifest),
            options_fingerprint=canonical_sha256_value(options),
            context_fingerprint=canonical_sha256_value(context),
            record_count=manifest.record_count,
            entity_count=manifest.entity_count,
            value_count=manifest.value_count,
            fields=tuple(result_fields),
            relationships=relationships,
            reasons=("pair_limit",) if evidence.pair_overflow else (),
            classification=maximum_classification(
                context.data_classification,
                *(f.pii.classification for f in result_fields),
            ),
            retained_state_bytes=ledger.peak,
            sample_bytes=sum(f.samples.bytes for f in fields.values()),
        )
        # Ранний бюджет учитывает разделители и резерв оболочки; перед публикацией
        # проверяем весь canonical DTO, включая metadata и fingerprints.
        if len(result.canonical_json().encode("utf-8")) > options.max_profile_bytes:
            raise limit("profile_bytes")
        self._deadline(end)
        return result

    def _deadline(self, end: float) -> None:
        if self.timer() >= end:
            raise failure("deadline", code="TIMEOUT")

    @staticmethod
    def _bind_context(
        context: NormalizedProfileContext, batch: NormalizedBatch
    ) -> None:
        for expected, actual in (
            (context.source_fingerprint, batch.source.source_fingerprint),
            (context.extraction_fingerprint, batch.extraction_fingerprint),
            (context.parse_plan_fingerprint, batch.parse_plan_fingerprint),
        ):
            if expected is not None and expected != actual:
                raise failure("context_lineage")

    async def _classify(
        self, request: PIIClassificationRequest
    ) -> PIIClassificationResult:
        try:
            result = await self.classifier.classify(request)
            preflight(result, self.options)
            checked = PIIClassificationResult.model_validate(
                result.model_dump(mode="python")
            )
            baseline = await LocalPIIClassifier().classify(request)
            if (
                checked.field != request.field
                or checked.input_fingerprint != request.input_fingerprint
                or classification_rank(checked.classification)
                < classification_rank(baseline.classification)
                or not set(baseline.categories) <= set(checked.categories)
                or (checked.complete and not baseline.complete)
            ):
                raise ValueError("classifier_contract")
            return checked
        except BaseException as error:
            # Только внешняя classifier boundary; control-flow exceptions сохраняются.
            if not isinstance(error, Exception):
                raise
            raise failure("classifier_failed", code="CLASSIFICATION_FAILED") from None

    def _field(
        self,
        stats: FieldStats,
        count: int,
        request: PIIClassificationRequest,
        pii: PIIClassificationResult,
    ) -> NormalizedFieldProfile:
        inference = stats.inference()
        distinct = stats.distinct.count(stats.non_null)
        raw = (
            self.options.examples == ExamplePolicy.LOCAL_RAW
            and pii.complete
            and pii.state == "not_detected"
            and classification_rank(pii.classification)
            <= classification_rank(DataClassification.INTERNAL)
        )
        examples = tuple(
            ProfileExample(
                ordinal=-ordinal,
                kind=value.kind,
                value=value if raw else None,
                masked=not raw,
            )
            for _, ordinal, value, _ in sorted(stats.samples.heap, key=lambda e: -e[1])
            if value.kind != "null"
        )
        metrics = stats.extrema.output()
        global_extrema = metrics[0] if len(metrics) == 1 else None
        candidates = (
            stats.parsed.output() if "currency_ambiguity" not in stats.reasons else ()
        )
        reasons = set(stats.reasons)
        if stats.samples.skipped:
            reasons.add("example_too_large")
        return NormalizedFieldProfile(
            field=stats.ref,
            declared_semantic_type=stats.declared,
            entity_count=count,
            present_count=stats.present,
            missing_count=count - stats.present,
            explicit_null_count=stats.nulls,
            non_null_count=stats.non_null,
            null_count=count - stats.non_null,
            null_ratio=ratio(count - stats.non_null, count),
            unique_count=distinct,
            unique_ratio=ratio(distinct, stats.non_null)
            if distinct is not None
            else None,
            unique_mode="estimated" if stats.distinct.overflow else "exact",
            distinct_k=self.options.distinct_k,
            observed_kinds=stats.kind_counts(),
            string_count=stats.strings,
            min_length=stats.min_length,
            max_length=stats.max_length,
            total_length=stats.total_length,
            mean_length=ratio(stats.total_length, stats.strings),
            minimum=global_extrema.minimum if global_extrema else None,
            maximum=global_extrema.maximum if global_extrema else None,
            extrema=metrics,
            candidate_extrema=candidates,
            patterns=stats.evidence(),
            inference=inference,
            identity=stats.identity(count, inference),
            categorical=stats.non_null >= 20
            and distinct is not None
            and not stats.distinct.overflow
            and distinct <= 20
            and distinct * 5 <= stats.non_null
            and not stats.skipped,
            examples=examples,
            sample_eligible=stats.samples.eligible,
            sample_skipped=stats.samples.skipped,
            pattern_checked=stats.checked,
            pattern_skipped=stats.skipped,
            labels=request.labels,
            context_available=bool(request.labels),
            reasons=tuple(sorted(reasons)),
            pii=pii,
        )
