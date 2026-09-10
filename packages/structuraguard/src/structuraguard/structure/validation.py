"""Строгая проверка ParsePlan с обязательным physical replay для acceptance."""

import asyncio
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, runtime_checkable

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import (
    IssueSeverity,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.execution import ExecutionStage, ParsePlanOptions
from structuraguard.contracts.parsing import (
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import ParseExecutionError
from structuraguard.structure._plan_check import (
    failure,
    prepare,
    validation_fingerprint,
)
from structuraguard.structure._runtime import PlanRuntime


@runtime_checkable
class AsyncClosable(Protocol):
    async def aclose(self) -> None: ...


@runtime_checkable
class SyncClosable(Protocol):
    def close(self) -> None: ...


async def close_source(
    iterator: AsyncIterator[ExtractedBatch], primary: BaseException | None = None
) -> None:
    if not isinstance(iterator, AsyncClosable):
        return
    try:
        async with asyncio.timeout(1):
            await iterator.aclose()
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        # Граница cleanup недоверенного iterator: содержимое его exception не
        # должно заменить primary issue или попасть в diagnostics.
        if not isinstance(error, Exception):
            raise
        if primary is not None:
            primary.add_note("PARSE_EXECUTION_SOURCE_ERROR: cleanup_failed")
        else:
            raise failure(
                "cleanup_failed",
                code="PARSE_EXECUTION_SOURCE_ERROR",
                stage=ExecutionStage.CLEANUP,
            ) from None


async def next_source(iterator: AsyncIterator[ExtractedBatch]) -> ExtractedBatch:
    try:
        return await anext(iterator)
    except StopAsyncIteration:
        raise
    except BaseException as error:
        # Только вызов внешнего iterator: чужой exception не становится raw
        # diagnostic. Cancellation и process-control исключения не подавляются.
        if not isinstance(error, Exception):
            raise
        raise failure(
            "source_read_failed",
            code="PARSE_EXECUTION_SOURCE_ERROR",
            stage=ExecutionStage.SOURCE,
        ) from None


def source_iterator(
    batches: AsyncIterable[ExtractedBatch],
) -> AsyncIterator[ExtractedBatch]:
    try:
        return aiter(batches)
    except BaseException as error:
        # Создание внешнего iterator имеет ту же boundary, что и чтение.
        if not isinstance(error, Exception):
            raise
        raise failure(
            "source_open_failed",
            code="PARSE_EXECUTION_SOURCE_ERROR",
            stage=ExecutionStage.SOURCE,
        ) from None


def sync_source_iterator(batches: Iterable[ExtractedBatch]) -> Iterator[ExtractedBatch]:
    try:
        return iter(batches)
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise failure(
            "source_open_failed",
            code="PARSE_EXECUTION_SOURCE_ERROR",
            stage=ExecutionStage.SOURCE,
        ) from None


class ParsePlanValidator:
    """Проверяет schema, закрытую policy и существование selections в source.

    Args:
        options: Конечные budgets/policy; executor должен использовать те же options.

    Raises:
        ParseExecutionError: Передан неверный тип options (PARSE_PLAN_INVALID).
        ValueError: Нарушен контракт значений options.

    Без replay ``validate`` возвращает REJECTED/REPLAY_REQUIRED. Для async
    parser stream используйте ``validate_source``. Wrapper сериализуем и не
    является capability; executor повторно проверяет policy и каждый batch.
    """

    def __init__(self, *, options: ParsePlanOptions | None = None) -> None:
        if options is not None and type(options) is not ParsePlanOptions:
            raise failure(
                "options_type",
                code="PARSE_PLAN_INVALID",
                stage=ExecutionStage.VALIDATION,
            )
        self.options = ParsePlanOptions.model_validate(
            (options or ParsePlanOptions()).model_dump(mode="python")
        )

    def validate(
        self,
        request: ParsePlanValidationRequest | dict[str, object],
        *,
        batches: Iterable[ExtractedBatch] | None = None,
    ) -> ParsePlanValidationResult:
        """Проверить DTO/decoded JSON и полный sync replay с bounded memory.

        Args:
            request: Plan, source reference, manifest и profile как DTO либо
                обычный decoded JSON dict; все поля считаются недоверенными.
            batches: Повторяемый extraction как sync iterable. Без него plan
                отклоняется с PARSE_PLAN_REPLAY_REQUIRED.

        Returns:
            ACCEPTED с validated_plan либо REJECTED с безопасными typed issues,
            включая ошибки schema, source, limits, timeout и cleanup.

        Метод потребляет iterator и вызывает его close-hook. Timeout cooperative:
        блокирующий next() не прерывается. Сеть, LLM, DB и пользовательский код
        из plan не исполняются; wrapper не отменяет проверок executor.
        Процессные исключения BaseException распространяются после cleanup.
        """
        checked: ParsePlanValidationRequest | None = None
        started = monotonic()
        try:
            checked = prepare(request, self.options)
            if batches is None:
                raise failure(
                    "physical_replay_required",
                    code="PARSE_PLAN_REPLAY_REQUIRED",
                    stage=ExecutionStage.VALIDATION,
                )
            runtime = PlanRuntime(checked, self.options)
            iterator = sync_source_iterator(batches)
            primary: BaseException | None = None
            try:
                while True:
                    try:
                        batch = next(iterator)
                    except StopIteration:
                        break
                    except BaseException as error:
                        if not isinstance(error, Exception):
                            raise
                        raise failure(
                            "source_read_failed",
                            code="PARSE_EXECUTION_SOURCE_ERROR",
                            stage=ExecutionStage.SOURCE,
                        ) from None
                    for _ in runtime.consume(batch):
                        if (
                            monotonic() - started
                            > self.options.source_limits.max_processing_seconds
                        ):
                            raise failure(
                                "validation_deadline",
                                code="PROCESSING_TIMEOUT",
                                stage=ExecutionStage.TIMEOUT,
                            )
                    if (
                        monotonic() - started
                        > self.options.source_limits.max_processing_seconds
                    ):
                        raise failure(
                            "validation_deadline",
                            code="PROCESSING_TIMEOUT",
                            stage=ExecutionStage.TIMEOUT,
                        )
                for _ in runtime.finish():
                    pass
            except BaseException as error:
                primary = error
                raise
            finally:
                if isinstance(iterator, SyncClosable):
                    try:
                        iterator.close()
                    except BaseException as error:
                        if not isinstance(error, Exception):
                            raise
                        if primary is not None:
                            primary.add_note(
                                "PARSE_EXECUTION_SOURCE_ERROR: cleanup_failed"
                            )
                        else:
                            raise failure(
                                "cleanup_failed",
                                code="PARSE_EXECUTION_SOURCE_ERROR",
                                stage=ExecutionStage.CLEANUP,
                            ) from None
        except ParseExecutionError as error:
            return self._result(checked, error)
        return self._result(checked)

    async def validate_source(
        self,
        request: ParsePlanValidationRequest | dict[str, object],
        batches: AsyncIterable[ExtractedBatch],
    ) -> ParsePlanValidationResult:
        """Проверить plan полным async replay и закрыть iterator с close-hook.

        Args:
            request: DTO либо decoded JSON dict с plan/source/manifest/profile.
            batches: Новый полный async stream того же extraction до EOF.

        Returns:
            ACCEPTED с validated_plan после успешного cleanup либо REJECTED
            с typed issues; source error и timeout не дают успешный wrapper.

        Raises:
            asyncio.CancelledError: Отмена вызывающего кода после cleanup.

        Весь source не сохраняется; memory и время ограничены options. Ошибки
        source не копируют raw message/details в result. Method не вызывает
        LLM, сеть или DB; plan operations ограничены закрытой policy.
        """
        checked: ParsePlanValidationRequest | None = None
        iterator: AsyncIterator[ExtractedBatch] | None = None
        primary: BaseException | None = None
        closed = False
        try:
            iterator = source_iterator(batches)
            async with asyncio.timeout(
                self.options.source_limits.max_processing_seconds
            ):
                checked = prepare(request, self.options)
                runtime = PlanRuntime(checked, self.options)
                while True:
                    try:
                        batch = await next_source(iterator)
                    except StopAsyncIteration:
                        break
                    for _ in runtime.consume(batch):
                        await asyncio.sleep(0)
                    await asyncio.sleep(0)
                for _ in runtime.finish():
                    await asyncio.sleep(0)
            closed = True
            await close_source(iterator)
            return self._result(checked)
        except ParseExecutionError as error:
            primary = error
            return self._result(checked, error)
        except TimeoutError:
            primary = failure(
                "validation_deadline",
                code="PROCESSING_TIMEOUT",
                stage=ExecutionStage.TIMEOUT,
            )
            return self._result(checked, primary)
        except BaseException as error:
            primary = error
            raise
        finally:
            if iterator is not None and not closed:
                await close_source(iterator, primary)

    def _result(
        self,
        request: ParsePlanValidationRequest | None,
        error: ParseExecutionError | None = None,
    ) -> ParsePlanValidationResult:
        unknown = "sha256:" + "0" * 64
        issues = (
            (
                ValidationIssue(
                    code=error.issue.code,
                    severity=IssueSeverity.ERROR,
                    message_key=error.issue.reason.upper(),
                ),
            )
            if error
            else ()
        )
        metadata = dict(
            validator_id="parse_plan_validator",
            validator_version="1.0.0",
            validated_at=datetime.now(UTC),
            validation_fingerprint=validation_fingerprint(request, self.options)
            if request
            else canonical_sha256_value(
                tuple(issue.canonical_json() for issue in issues)
            ),
            source_fingerprint=request.source.source_fingerprint
            if request
            else unknown,
            extraction_fingerprint=request.manifest.extraction_fingerprint
            if request
            else unknown,
            profile_fingerprint=request.profile.profile_fingerprint
            if request
            else unknown,
            plan_fingerprint=request.plan.fingerprint if request else unknown,
            issues=issues,
        )
        accepted = (
            ValidatedParsePlan.model_validate({"plan": request.plan, **metadata})
            if request is not None and error is None
            else None
        )
        return ParsePlanValidationResult.model_validate(
            {
                **metadata,
                "decision": ValidationDecision.ACCEPTED
                if accepted
                else ValidationDecision.REJECTED,
                "validated_plan": accepted,
            }
        )
