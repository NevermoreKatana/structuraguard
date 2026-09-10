"""Async session: validated plan, provisional batches и terminal semantic report."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from time import monotonic
from types import TracebackType
from typing import Self

from structuraguard.contracts.common import (
    PipelineStatus,
    ProducerMetadata,
    SemanticParsingMode,
    ValidationDecision,
)
from structuraguard.contracts.llm import LLMErrorCode
from structuraguard.contracts.normalized import NormalizedBatch
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlan,
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
)
from structuraguard.contracts.reports import SemanticParseReport
from structuraguard.contracts.semantic import LLMAnalysisContext, ParsingPolicy
from structuraguard.exceptions import LLMProviderError, StructuraGuardError
from structuraguard.llm.run import utc_now
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.security import SecurityScanner
from structuraguard.structure.execution import ParsePlanExecutor
from structuraguard.structure.hybrid import (
    HybridAnalysis,
    HybridStructureAnalyzer,
    assess,
    semantic_issue,
)
from structuraguard.structure.semantic_samples import Replay, open_replay
from structuraguard.structure.validation import ParsePlanValidator


class SemanticParsingSession:
    """Один semantic run над replay одного immutable extraction snapshot.

    ``replay`` открывает независимые потоки тех же M4 batches с terminal manifest;
    ``context`` задаёт доверенные run ID, classification и routing lineage.
    ``policy`` ограничивает samples/chunks, calls/tokens/time и output; по умолчанию
    выбран ``llm_assisted``. Для LLM нужны конкретный ``provider`` и доверенный
    ``scanner``; отсутствие разрешения не допускает generation. ``clock`` возвращает
    UTC datetime, ``timer`` — монотонные секунды; оба можно контролировать в tests.

    Session используется последовательно для одного execution. Она владеет только
    открытыми replay/output iterators; provider lifecycle принадлежит caller.
    До terminal manifest все batches предварительные. NEEDS_REVIEW возвращает
    подтверждённые records без terminal и без normalized_fingerprint в report.
    Caller закрывает iterator при раннем выходе либо использует async with.
    Ошибка или cancellation анализа закрывает session, не позволяя сбросить budget.
    Конструктор не выполняет I/O; invalid policy/context дают Pydantic ValidationError.
    DB mapping и доступ модели к tools, БД или filesystem отсутствуют.
    """

    def __init__(
        self,
        *,
        replay: Replay,
        context: LLMAnalysisContext,
        policy: ParsingPolicy | None = None,
        provider: LLMProvider | None = None,
        scanner: SecurityScanner | None = None,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self._analyzer = HybridStructureAnalyzer(
            context=context,
            policy=policy,
            provider=provider,
            scanner=scanner,
            clock=clock,
            timer=timer,
        )
        self.policy, self.context = self._analyzer.policy, self._analyzer.context
        self._replay, self._clock = replay, clock
        self._analysis: HybridAnalysis | None = None
        self._report: SemanticParseReport | None = None
        self._active: AsyncGenerator[NormalizedBatch, None] | None = None
        self._closed = self._started = self._analyzing = False
        self._records = self._nonnull = self._grounded = 0

    @property
    def report(self) -> SemanticParseReport | None:
        """Вернуть итог потребления parsing iterator либо None.

        Report требует подтверждённого physical snapshot; одного анализа структуры
        недостаточно. EOF, failure, cancellation или закрытие preview фиксируют
        доступный outcome. Plan/source refs могут быть sensitive: весь report
        не предназначен для безопасного audit log.
        """
        return self._report

    async def __aenter__(self) -> Self:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Закрыть output/replay и запретить новые операции session.

        Для начатого preview формируется CANCELLED, если snapshot уже подтверждён.
        Ошибки cleanup распространяются; внешний provider не закрывается. Caller
        завершает активную итерацию перед закрытием, конкурентное использование
        session не поддерживается.
        """
        if self._active is not None:
            await self._active.aclose()
        self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)

    async def analyze_structure(self) -> HybridAnalysis:
        """Прочитать replay и вернуть кэшируемый HybridAnalysis с plan и issues.

        Выбор LLM определяется policy; успешный повтор не расходует calls. Issues
        и draft не означают готовый dataset, report создаётся parsing iterator.
        Source/deadline ошибки и cancellation распространяются и закрывают session.
        Закрытая session даёт LLM_REQUEST_INVALID, конкурентный анализ —
        LLM_POLICY_DENIED через LLMProviderError.
        """
        self._ensure_open()
        if self._analyzing:
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        if self._analysis is None:
            self._analyzing = True
            try:
                self._analysis = await self._analyzer.analyze_source(self._replay)
            except BaseException:
                # Повтор после late failure/cancellation создал бы новый LLM budget
                # под прежним run ID. Для нового анализа нужна отдельная session.
                self._closed = True
                raise
            finally:
                self._analyzing = False
        return self._analysis

    async def create_parse_plan(self) -> ParsePlan | None:
        """Вернуть draft из analyze_structure либо None, без повторной generation.

        Issues и assessment остаются в HybridAnalysis: plan с review issues нельзя
        считать подтверждённым dataset. I/O и исключения совпадают с анализом.
        """
        return (await self.analyze_structure()).plan

    async def validate_parse_plan(self, plan: ParsePlan) -> ParsePlanValidationResult:
        """Проверить недоверенный plan против полного replay без вызова LLM.

        Возвращает decision/issues и optional validated_plan от общего M5 validator.
        При отсутствии кэша profile строится детерминированно. Источник должен
        сохранить IDs, lineage и content; прежний plan fingerprint не разрешает
        execution автоматически. Source ошибки и cancellation распространяются;
        закрытая session даёт LLMProviderError с LLM_REQUEST_INVALID.
        """
        self._ensure_open()
        result = self._analysis or await self._analyzer.analyze_source(
            self._replay, mode=SemanticParsingMode.DETERMINISTIC
        )
        return await ParsePlanValidator(
            options=self.policy.structural.execution, clock=self._clock
        ).validate_source(
            ParsePlanValidationRequest(
                plan=plan,
                source=result.manifest.source,
                manifest=result.manifest,
                profile=result.profile,
            ),
            open_replay(self._replay),
        )

    def parse_semantically(
        self, *, plan: ParsePlan | None = None
    ) -> AsyncGenerator[NormalizedBatch, None]:
        """Вернуть единственный async iterator предварительных NormalizedBatch.

        ``plan=None`` использует кэш или запускает анализ по policy. Переданный plan
        повторно проверяется и применяется без новой generation. Работа с source
        начинается при итерации; ненулевые values обязаны иметь provenance.
        Только terminal batch подтверждает dataset. При NEEDS_REVIEW terminal нет,
        при отсутствии plan records не выдаются; причину содержит report.

        Source/execution ошибки и cancellation распространяются при итерации;
        FAILED/CANCELLED report возможен только после подтверждения snapshot.
        Повтор execution даёт LLM_POLICY_DENIED, закрытая session —
        LLM_REQUEST_INVALID через LLMProviderError. Iterator нужно исчерпать или
        закрыть; provider lifecycle остаётся у caller.
        """
        self._ensure_open()
        if self._started:
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED)
        self._started = True
        self._active = self._parse(plan)
        return self._active

    async def _parse(
        self, saved_plan: ParsePlan | None
    ) -> AsyncGenerator[NormalizedBatch, None]:
        output: AsyncGenerator[NormalizedBatch, None] | None = None
        try:
            if saved_plan is not None and (
                self._analysis is None or self._analysis.plan != saved_plan
            ):
                previous_calls = (
                    self._analysis.provider_metadata if self._analysis else ()
                )
                self._analysis = await self._analyzer.validate_saved(
                    self._replay, saved_plan
                )
                self._analysis = replace(
                    self._analysis, provider_metadata=previous_calls
                )
            result = await self.analyze_structure()
            if result.plan is None:
                status = (
                    PipelineStatus.REJECTED_SECURITY
                    if any(i.code == LLMErrorCode.UNSAFE_CONTENT for i in result.issues)
                    else PipelineStatus.NEEDS_REVIEW
                )
                self._finish(status)
                return
            validation = await self.validate_parse_plan(result.plan)
            if (
                validation.decision is not ValidationDecision.ACCEPTED
                or validation.validated_plan is None
            ):
                self._analysis = replace(
                    result,
                    plan=None,
                    issues=(*result.issues, *validation.issues),
                    assessment=assess(),
                )
                self._finish(PipelineStatus.NEEDS_REVIEW)
                return
            output = ParsePlanExecutor(
                options=self.policy.structural.execution
            ).execute(
                open_replay(self._replay),
                validation.validated_plan,
                ParseExecutionContext(
                    run_id=self.context.run_id,
                    source_fingerprint=result.manifest.source.source_fingerprint,
                    extraction_fingerprint=result.manifest.extraction_fingerprint,
                    parse_plan_fingerprint=result.plan.fingerprint,
                    manifest=result.manifest,
                    profile=result.profile,
                    max_records_per_batch=self.policy.records_per_batch,
                ),
            )
            async for batch in output:
                self._records += len(batch.records)
                for record in batch.records:
                    for entity in record.entities:
                        for value in entity.values:
                            if value.normalized_value.value is not None:
                                self._nonnull += 1
                                self._grounded += bool(
                                    value.source_refs and value.origins
                                )
                if batch.is_last:
                    if result.issues:
                        self._finish(PipelineStatus.NEEDS_REVIEW)
                        if batch.records:
                            data = batch.model_dump()
                            data.update(
                                is_last=False,
                                manifest=None,
                                batch_fingerprint="sha256:" + "0" * 64,
                            )
                            yield NormalizedBatch.model_validate(data)
                    else:
                        assert batch.manifest is not None
                        self._finish(
                            PipelineStatus.COMPLETED,
                            batch.manifest.normalized_fingerprint,
                        )
                        yield batch
                else:
                    yield batch
        except (asyncio.CancelledError, GeneratorExit):
            if self._report is None:
                self._fail(PipelineStatus.CANCELLED, "SEMANTIC_CANCELLED")
            raise
        except StructuraGuardError as error:
            self._fail(PipelineStatus.FAILED, error.error_code)
            raise
        except TimeoutError:
            self._fail(PipelineStatus.FAILED, "SEMANTIC_TIMEOUT")
            raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
        finally:
            if output is not None:
                await output.aclose()

    def _fail(self, status: PipelineStatus, code: str) -> None:
        if self._analysis is None:
            self._analysis = self._analyzer.last_analysis
        if self._analysis is not None:
            issues = (*self._analysis.issues, semantic_issue(code))
            if len(issues) > self.policy.max_issues:
                # При заполненном budget сначала сохраняем security veto, затем
                # terminal причину. Она не должна сломать создание FAILED/CANCELLED
                # report; при единственном слоте status всё равно отражает outcome.
                issues = tuple(
                    sorted(
                        issues,
                        key=lambda issue: (
                            issue.code != LLMErrorCode.UNSAFE_CONTENT,
                            issue.code != code,
                        ),
                    )
                )[: self.policy.max_issues]
            self._analysis = replace(self._analysis, issues=issues)
            self._finish(status)

    def _finish(self, status: PipelineStatus, fingerprint: str | None = None) -> None:
        result = self._analysis
        assert result is not None
        calls = result.provider_metadata
        refs = tuple(
            dict.fromkeys(
                (
                    *result.source_refs,
                    *(ref for issue in result.issues for ref in issue.source_refs),
                )
            )
        )
        self._report = SemanticParseReport(
            schema_version="1.1.0",
            run_id=self.context.run_id,
            producer=ProducerMetadata(
                component_id="semantic_parsing_session",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
            status=status,
            source_fingerprint=result.manifest.source.source_fingerprint,
            extraction_fingerprint=result.manifest.extraction_fingerprint,
            parse_plan_fingerprint=result.plan.fingerprint if result.plan else None,
            plan=result.plan,
            mode=self.policy.mode,
            assessment=result.assessment,
            provider_metadata=calls,
            source_refs=refs,
            unresolved_refs=result.unresolved_refs,
            input_tokens=None
            if any(call.input_tokens is None for call in calls)
            else sum(call.input_tokens or 0 for call in calls),
            output_tokens=None
            if any(call.output_tokens is None for call in calls)
            else sum(call.output_tokens or 0 for call in calls),
            normalized_fingerprint=fingerprint,
            records=self._records,
            unresolved_blocks=result.unresolved_blocks,
            provenance_coverage=Decimal(self._grounded) / Decimal(self._nonnull)
            if self._nonnull
            else Decimal(1)
            if result.plan is not None
            else Decimal(0),
            llm_calls=len(calls),
            issues=result.issues,
            generated_at=self._clock(),
        )
