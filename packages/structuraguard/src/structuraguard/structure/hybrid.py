"""M5 candidates → bounded LLM при необходимости → независимая оценка и validation."""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Context, Decimal, localcontext
from time import monotonic

from structuraguard.contracts.analysis import StructureAnalysisOptions
from structuraguard.contracts.common import (
    IssueSeverity,
    ParsePlanKind,
    PhysicalObjectKind,
    PhysicalSourceRef,
    RawScalar,
    SemanticParsingMode,
    StringScalar,
    ValidationDecision,
    ValidationIssue,
)
from structuraguard.contracts.document_semantics import (
    DocumentSpanGrouping,
    DocumentSpanSelector,
)
from structuraguard.contracts.llm import LLMCallRecord, LLMErrorCode
from structuraguard.contracts.parsing import (
    ParsePlan,
    ParsePlanValidationRequest,
    PhysicalSample,
    StructureAnalysisRequest,
    StructureAnalysisResult,
    StructureNeedsReview,
    StructureNeedsSemanticAnalysis,
    StructurePlanCreated,
    StructureProfile,
    StructureRejected,
)
from structuraguard.contracts.semantic import (
    LLMAnalysisContext,
    ParsingPolicy,
    SemanticConfidence,
)
from structuraguard.contracts.source import (
    ExtractedBatch,
    ExtractedDatasetManifest,
    PhysicalNodeKind,
    SourceLocation,
)
from structuraguard.exceptions import LLMProviderError
from structuraguard.llm.run import LLMRunProvider, utc_now
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.security import SecurityScanner
from structuraguard.structure.analysis import DeterministicStructureAnalyzer
from structuraguard.structure.chunking import ChunkedSource
from structuraguard.structure.document_entities import DocumentEntityExtractor
from structuraguard.structure.llm_analysis import LLMStructureAnalyzer
from structuraguard.structure.semantic_samples import (
    Replay,
    open_replay,
)
from structuraguard.structure.validation import (
    ParsePlanValidator,
    close_source,
    next_source,
    source_iterator,
)


def semantic_issue(
    code: str, refs: tuple[PhysicalSourceRef, ...] = ()
) -> ValidationIssue:
    """Machine-readable issue без source text и provider error body."""
    return ValidationIssue(
        code=code, severity=IssueSeverity.WARNING, message_key=code, source_refs=refs
    )


def assess(
    evidence: Decimal = Decimal(0),
    validation: Decimal = Decimal(0),
    agreement: Decimal = Decimal(0),
    penalty: Decimal = Decimal(0),
) -> SemanticConfidence:
    """Фиксированная decimal context не зависит от ambient precision caller."""
    with localcontext(Context(prec=28)):
        confidence = max(
            Decimal(0),
            evidence * Decimal("0.2")
            + validation * Decimal("0.5")
            + agreement * Decimal("0.3")
            - penalty,
        )
    return SemanticConfidence(
        deterministic_evidence=evidence,
        validation=validation,
        agreement=agreement,
        penalty=penalty,
        confidence=confidence,
    )


@dataclass(frozen=True, slots=True)
class HybridAnalysis:
    """Результат анализа snapshot с optional plan, assessment и bounded issues.

    Manifest/profile задают проверенный source lineage; unresolved refs сохраняют
    непокрытый scope. Issues запрещают подтверждение полного dataset независимо
    от confidence. Только provider_metadata предназначены для safe diagnostics:
    plan и source refs могут содержать sensitive names/paths. Records здесь нет.
    """

    profile: StructureProfile
    manifest: ExtractedDatasetManifest
    plan: ParsePlan | None
    assessment: SemanticConfidence
    issues: tuple[ValidationIssue, ...] = ()
    source_refs: tuple[PhysicalSourceRef, ...] = ()
    unresolved_refs: tuple[PhysicalSourceRef, ...] = ()
    unresolved_blocks: int = 0
    provider_metadata: tuple[LLMCallRecord, ...] = ()


def _sample_values(
    batch: ExtractedBatch,
) -> Iterator[tuple[PhysicalSourceRef, RawScalar, SourceLocation]]:
    """Отбор scalar samples не применяет ограничения LLM paths к deterministic run."""

    def ref(kind: PhysicalObjectKind, identifier: str) -> PhysicalSourceRef:
        return PhysicalSourceRef(
            extraction_id=batch.extraction_id,
            batch_index=batch.batch_index,
            kind=kind,
            local_id=identifier,
        )

    for table in batch.tables:
        for cell in table.cells:
            yield (
                ref(PhysicalObjectKind.CELL, cell.cell_id),
                cell.value.raw_value,
                cell.value.location,
            )
    for node in batch.trees:
        if node.value is not None:
            yield (
                ref(PhysicalObjectKind.TREE_NODE, node.node_id),
                node.value.raw_value,
                node.location,
            )
    for line in (
        *batch.lines,
        *(line for block in batch.blocks for line in block.lines),
    ):
        yield (
            ref(PhysicalObjectKind.LINE, line.line_id),
            StringScalar(value=line.text),
            line.location,
        )
    for block in batch.blocks:
        if block.text is not None:
            yield (
                ref(PhysicalObjectKind.BLOCK, block.block_id),
                StringScalar(value=block.text),
                block.location,
            )


class _Capture:
    def __init__(self, policy: ParsingPolicy) -> None:
        self.policy = policy
        self.manifest: ExtractedDatasetManifest | None = None
        self.samples: list[PhysicalSample] = []
        self.tail: deque[tuple[PhysicalSample, int]] = deque()
        self.bytes = 0
        self.tail_bytes = 0
        self.document = False

    async def read(self, replay: Replay) -> AsyncGenerator[ExtractedBatch, None]:
        iterator = source_iterator(open_replay(replay))
        primary: BaseException | None = None
        try:
            while True:
                try:
                    batch = await next_source(iterator)
                except StopAsyncIteration:
                    break
                # M5 проверяет raw batch прежде, чем следующий resume сохраняет samples.
                yield batch
                self.document |= (
                    any(block.kind.value != "line" for block in batch.blocks)
                    or batch.parser_id == "builtin.text"
                    or any(
                        node.node_kind is PhysicalNodeKind.ELEMENT
                        for node in batch.trees
                    )
                )
                for ref, raw, location in _sample_values(batch):
                    cap = min(65536, self.policy.structural.max_payload_bytes // 2)
                    if (
                        raw is None
                        or ref.kind.value == "value"
                        or (
                            isinstance(raw.value, str | bytes)
                            and len(raw.value) > cap // 2
                        )
                    ):
                        continue
                    sample = PhysicalSample(
                        source_ref=ref,
                        raw_value=raw,
                        location=location,
                        batch_fingerprint=batch.batch_fingerprint,
                    )
                    size = len(sample.canonical_json().encode())
                    head_count = max(1, self.policy.structural.max_sample_values // 2)
                    if len(self.samples) < head_count and self.bytes + size <= cap // 2:
                        self.samples.append(sample)
                        self.bytes += size
                    elif size <= cap // 2:
                        tail_count = self.policy.structural.max_sample_values - len(
                            self.samples
                        )
                        while self.tail and (
                            len(self.tail) >= tail_count
                            or self.tail_bytes + size > cap - self.bytes
                        ):
                            self.tail_bytes -= self.tail.popleft()[1]
                        if tail_count:
                            self.tail.append((sample, size))
                            self.tail_bytes += size
                if batch.manifest is not None:
                    self.manifest = batch.manifest
                await asyncio.sleep(0)
            self.samples.extend(sample for sample, _ in self.tail)
            self.tail.clear()
        except BaseException as error:
            primary = error
            raise
        finally:
            await close_source(iterator, primary)


class HybridStructureAnalyzer:
    """Выбрать и проверить semantic plan с конечным LLM budget на один анализ.

    ``context`` задаёт доверенные run ID/classification, ``policy`` — режим и limits
    (default llm_assisted). Для generation нужны ``provider`` и trusted ``scanner``;
    ``clock``/``timer`` задают UTC timestamp/монотонные секунды. Конструктор не делает
    I/O и проверяет DTO через Pydantic, invalid inputs дают ValidationError.
    Повторяющиеся table/tree/log records используют один structural proposal.
    Document extraction допускает один request на bounded chunk. Provider и его
    HTTP lifecycle принадлежат caller. Между вызовами остаётся last_analysis,
    но не raw source. Каждый analyze_source создаёт новый budget; для кэша одного
    run используется SemanticParsingSession. DB mapping/tools отсутствуют.
    """

    def __init__(
        self,
        *,
        context: LLMAnalysisContext,
        policy: ParsingPolicy | None = None,
        provider: LLMProvider | None = None,
        scanner: SecurityScanner | None = None,
        clock: Callable[[], datetime] = utc_now,
        timer: Callable[[], float] = monotonic,
    ) -> None:
        self.policy = ParsingPolicy.model_validate(
            (policy or ParsingPolicy()).model_dump()
        )
        self.context = LLMAnalysisContext.model_validate(context.model_dump())
        self.provider, self.scanner, self.clock, self.timer = (
            provider,
            scanner,
            clock,
            timer,
        )
        self.last_analysis: HybridAnalysis | None = None

    async def analyze(
        self, request: StructureAnalysisRequest, *, replay: Replay | None = None
    ) -> StructureAnalysisResult:
        """Вернуть protocol outcome после проверки request и полного replay.

        Request содержит ожидаемые source/profile/manifest и mode; replay должен
        возвращать тот же snapshot. Без replay возвращается StructureRejected
        без LLM. Deterministic policy запрещает override через request.mode.
        Source mismatch даёт LLM_REQUEST_INVALID через LLMProviderError; source
        ошибки, deadline и cancellation распространяются. Ambiguity/LLM failures
        отражаются в review/rejected outcome, подробности — в last_analysis.
        """
        checked = StructureAnalysisRequest.model_validate(
            request.model_dump(warnings="error")
        )
        if replay is None:
            return StructureRejected(
                profile=checked.profile,
                issues=(semantic_issue("STRUCTURE_REPLAY_REQUIRED"),),
            )
        async with asyncio.timeout(
            self.policy.structural.execution.source_limits.max_processing_seconds
        ):
            mode = (
                SemanticParsingMode.DETERMINISTIC
                if self.policy.mode is SemanticParsingMode.DETERMINISTIC
                else checked.mode
            )
            result = await self._analyze(replay, mode, expected=checked)
        if (
            result.profile.source != checked.source
            or result.manifest != checked.manifest
        ):
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
        if result.plan is not None and not result.issues:
            return StructurePlanCreated(profile=result.profile, plan=result.plan)
        issues = tuple(semantic_issue(i.code) for i in result.issues) or (
            semantic_issue("SEMANTIC_NEEDS_REVIEW"),
        )
        if any(issue.code == LLMErrorCode.UNSAFE_CONTENT for issue in issues):
            return StructureRejected(profile=result.profile, issues=issues)
        if result.profile.candidates:
            return StructureNeedsReview(
                profile=result.profile,
                candidates=result.profile.candidates,
                issues=issues,
            )
        return StructureNeedsSemanticAnalysis(
            profile=result.profile, issues=issues[:16]
        )

    async def analyze_source(
        self, replay: Replay, *, mode: SemanticParsingMode | None = None
    ) -> HybridAnalysis:
        """Прочитать replay и вернуть HybridAnalysis с проверенным draft и issues.

        ``mode`` переопределяет policy.mode, кроме явного deterministic запрета.
        Replay вызывается несколько раз; все streams должны описывать один
        immutable snapshot. LLM получает только bounded samples/chunks после
        approval. Ошибки generation становятся issues; source/deadline ошибки
        и cancellation распространяются. Пустой source даёт SOURCE_EMPTY без plan
        и calls. Каждый вызов начинает новый budget, результаты не кэшируются.
        """
        async with asyncio.timeout(
            self.policy.structural.execution.source_limits.max_processing_seconds
        ):
            effective = (
                SemanticParsingMode.DETERMINISTIC
                if self.policy.mode is SemanticParsingMode.DETERMINISTIC
                else mode or self.policy.mode
            )
            return await self._analyze(replay, effective)

    async def validate_saved(self, replay: Replay, plan: ParsePlan) -> HybridAnalysis:
        """Проверить сохранённый plan по полному replay без LLM и вернуть analysis.

        Plan считается недоверенным: проверяются lineage, source spans, policy
        и scope. Неудачная validation возвращает issues без plan; неполный scope
        остаётся review даже при принятом draft. Прежний score не разрешает новый
        run. Document draft с penalty сохраняет review даже при совпадении spans.
        Общий deadline охватывает все replay и cleanup; source ошибки и cancellation
        распространяются.
        """
        loop = asyncio.get_running_loop()
        deadline = (
            loop.time()
            + self.policy.structural.execution.source_limits.max_processing_seconds
        )
        async with asyncio.timeout_at(deadline):
            result = await self._validate_saved(replay, plan)
            if loop.time() >= deadline:
                raise TimeoutError
            return result

    async def _validate_saved(self, replay: Replay, plan: ParsePlan) -> HybridAnalysis:
        result = await self.analyze_source(
            replay, mode=SemanticParsingMode.DETERMINISTIC
        )
        validator = ParsePlanValidator(
            options=self.policy.structural.execution, clock=self.clock
        )
        validation = await validator.validate_source(
            {
                "plan": plan,
                "source": result.manifest.source,
                "manifest": result.manifest,
                "profile": result.profile,
            },
            open_replay(replay),
        )
        if validation.validated_plan is None:
            return replace(
                result, plan=None, issues=validation.issues, assessment=assess()
            )
        plan = validation.validated_plan.plan
        if ParsePlanKind(plan.kind) not in self.policy.structural.allowed_kinds:
            return replace(
                result,
                plan=None,
                issues=(semantic_issue("SEMANTIC_KIND_DENIED"),),
                assessment=assess(),
            )
        spans = tuple(
            span
            for field in plan.fields
            if isinstance(field.selector, DocumentSpanSelector)
            for span in field.selector.spans
        )
        if spans:
            review_required = (
                plan.final_assessment is not None and plan.final_assessment.penalty > 0
            )
            anchors = tuple(
                e.grouping.anchor
                for e in plan.entities
                if isinstance(e.grouping, DocumentSpanGrouping)
            )
            source = ChunkedSource(result.manifest, self.policy)
            unresolved_set: set[PhysicalSourceRef] = set()
            chunks = source.chunks(replay)
            try:
                async for chunk in chunks:
                    for fragment in chunk.fragments:
                        if not any(
                            span.source_ref == fragment.ref
                            and span.start < fragment.start + len(fragment.text)
                            and span.end > fragment.start
                            for span in (*spans, *anchors)
                        ):
                            unresolved_set.add(fragment.ref)
            finally:
                await chunks.aclose()
            if review_required:
                # Пересечение spans доказывает grounding, но не разрешает прежнюю
                # неоднозначность. Точные blockers не входят в saved plan: сохраняем
                # консервативный review всего доступного document scope.
                unresolved_set.update(source.refs)
            unresolved = tuple(ref for ref in source.refs if ref in unresolved_set)
            issues = (
                (semantic_issue("SEMANTIC_UNRESOLVED_SOURCE", unresolved[:64]),)
                if unresolved or source.omitted
                else ()
            )
            if review_required:
                issues = (semantic_issue("SEMANTIC_SAVED_SCOPE_NEEDS_REVIEW"), *issues)
            with localcontext(Context(prec=28)):
                coverage = (
                    Decimal(len(source.refs) - len(unresolved))
                    / Decimal(len(source.refs) + source.omitted)
                    if source.refs or source.omitted
                    else Decimal(0)
                )
            score = assess(
                coverage,
                Decimal(1),
                Decimal("0.85"),
                Decimal("0.2") if issues else Decimal(0),
            )
            if score.confidence < self.policy.structural.confidence_threshold:
                issues = (*issues, semantic_issue("SEMANTIC_CONFIDENCE_LOW"))
            return HybridAnalysis(
                result.profile,
                result.manifest,
                plan,
                score,
                issues[: self.policy.max_issues],
                tuple(source.refs),
                unresolved,
                len(unresolved) + source.omitted,
            )
        agrees = result.plan is not None and self._shape(result.plan) == self._shape(
            plan
        )
        evidence = result.plan.confidence if result.plan else Decimal(0)
        return replace(
            result,
            plan=plan,
            assessment=assess(
                evidence, Decimal(1), Decimal(1) if agrees else Decimal(0)
            ),
            issues=()
            if agrees
            else (semantic_issue("SEMANTIC_SAVED_SCOPE_NEEDS_REVIEW"),),
        )

    async def _analyze(
        self,
        replay: Replay,
        mode: SemanticParsingMode,
        *,
        expected: StructureAnalysisRequest | None = None,
    ) -> HybridAnalysis:
        capture = _Capture(self.policy)
        reader = capture.read(replay)
        try:
            deterministic = await DeterministicStructureAnalyzer(
                options=StructureAnalysisOptions(
                    confidence_threshold=self.policy.structural.confidence_threshold,
                    profiling=self.policy.structural.execution.source_limits,
                    max_plan_bytes=self.policy.structural.execution.max_plan_bytes,
                )
            ).analyze(reader)
        finally:
            await reader.aclose()
        manifest = capture.manifest
        if manifest is None:
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
        profile = deterministic.profile
        if expected is not None and (
            expected.manifest != manifest or expected.source != profile.source
        ):
            raise LLMProviderError(LLMErrorCode.REQUEST_INVALID)
        unresolved = tuple(manifest.source_index.refs[: self.policy.max_report_refs])
        self.last_analysis = HybridAnalysis(
            profile,
            manifest,
            None,
            assess(),
            (semantic_issue("SEMANTIC_ANALYSIS_INCOMPLETE"),),
            source_refs=unresolved,
            unresolved_refs=unresolved,
            unresolved_blocks=len(unresolved),
        )
        if not any(batch.physical_ref_count for batch in manifest.batches):
            return replace(self.last_analysis, issues=(semantic_issue("SOURCE_EMPTY"),))
        known = set(manifest.source_index.refs)
        samples = (
            expected.samples
            if expected is not None
            else tuple(s for s in capture.samples if s.source_ref in known)
        )
        validator = ParsePlanValidator(
            options=self.policy.structural.execution, clock=self.clock
        )
        base = (
            deterministic.plan
            if isinstance(deterministic, StructurePlanCreated)
            else None
        )
        if (
            base is not None
            and mode is not SemanticParsingMode.LLM_FIRST
            and (not capture.document or mode is SemanticParsingMode.DETERMINISTIC)
        ):
            return await self._finalize(
                profile,
                manifest,
                base,
                replay,
                validator,
                assess(base.confidence, Decimal(1), Decimal(1)),
            )
        if mode is SemanticParsingMode.DETERMINISTIC:
            assert self.last_analysis is not None
            return replace(
                self.last_analysis,
                issues=(semantic_issue("NEEDS_SEMANTIC_ANALYSIS", unresolved[:64]),),
            )
        if self.provider is None or self.scanner is None:
            assert self.last_analysis is not None
            return replace(
                self.last_analysis,
                issues=(semantic_issue(LLMErrorCode.POLICY_DENIED, unresolved[:64]),),
            )
        run = LLMRunProvider(
            self.provider, self.policy.budget, clock=self.clock, monotonic=self.timer
        )
        issues: tuple[ValidationIssue, ...] = ()
        try:
            if (
                capture.document
                and ParsePlanKind.DOCUMENT in self.policy.structural.allowed_kinds
            ):
                doc = await DocumentEntityExtractor(
                    provider=run,
                    scanner=self.scanner,
                    context=self.context,
                    policy=self.policy,
                ).extract(profile, manifest, replay)
                score = assess(
                    doc.evidence if doc.plan else Decimal(0),
                    Decimal(1) if doc.plan else Decimal(0),
                    doc.agreement if doc.plan else Decimal(0),
                    Decimal("0.2") if doc.issues else Decimal(0),
                )
                return await self._finalize(
                    profile,
                    manifest,
                    doc.plan,
                    replay,
                    validator,
                    score,
                    doc.issues,
                    doc.source_refs,
                    doc.unresolved_refs,
                    doc.unresolved_blocks,
                    run.calls,
                )
            if not samples:
                raise LLMProviderError(LLMErrorCode.CONTEXT_LIMIT)
            request = StructureAnalysisRequest(
                source=manifest.source,
                manifest=manifest,
                profile=profile,
                samples=samples,
                max_sample_values=self.policy.structural.max_sample_values,
                mode=mode,
            )
            proposed = await LLMStructureAnalyzer(
                provider=run,
                scanner=self.scanner,
                validator=validator,
                context=self.context,
                policy=self.policy.structural,
            ).propose(request, replay=replay)
            if isinstance(proposed, StructurePlanCreated):
                evidence = proposed.plan.confidence
                agreement = (
                    Decimal(1)
                    if base is not None
                    and self._shape(base) == self._shape(proposed.plan)
                    else Decimal("0.85")
                    if evidence >= Decimal("0.85")
                    else Decimal("0.5")
                )
                issues = (
                    (semantic_issue("SEMANTIC_PLAN_DISAGREEMENT"),)
                    if base is not None
                    and self._shape(base) != self._shape(proposed.plan)
                    else ()
                )
                if len(profile.candidates) > 1:
                    issues = (
                        *issues,
                        semantic_issue("SEMANTIC_CANDIDATE_DISAGREEMENT"),
                    )
                return await self._finalize(
                    profile,
                    manifest,
                    proposed.plan,
                    replay,
                    validator,
                    assess(
                        evidence,
                        Decimal(1),
                        agreement,
                        Decimal("0.2") if issues else Decimal(0),
                    ),
                    issues,
                    calls=run.calls,
                )
            issues = proposed.issues
        except LLMProviderError as error:
            issues = (semantic_issue(error.error_code),)
        finally:
            if self.last_analysis is not None:
                self.last_analysis = replace(
                    self.last_analysis, provider_metadata=run.calls
                )
        # Fallback сохраняет причину и требует review; cloud/policy не обходятся.
        if base is not None and not any(
            i.code in {LLMErrorCode.UNSAFE_CONTENT, LLMErrorCode.POLICY_DENIED}
            for i in issues
        ):
            return await self._finalize(
                profile,
                manifest,
                base,
                replay,
                validator,
                assess(base.confidence, Decimal(1), Decimal(1), Decimal("0.2")),
                issues,
                calls=run.calls,
            )
        assert self.last_analysis is not None
        return replace(
            self.last_analysis,
            issues=(
                *issues,
                semantic_issue("SEMANTIC_UNRESOLVED_SOURCE", unresolved[:64]),
            )[: self.policy.max_issues],
            provider_metadata=run.calls,
        )

    @staticmethod
    def _shape(plan: ParsePlan) -> str:
        """Сравнить только executable grammar, без IDs, names, score и provenance."""
        from structuraguard.contracts._base import canonical_json_value

        data = plan.model_dump(
            include={
                "kind",
                "table_ref",
                "header_row",
                "data_start_row",
                "data_end_row",
                "footer_start_row",
                "repeated_header_rows",
                "root_ref",
                "record_path",
                "line_refs",
                "max_lines_per_record",
                "block_refs",
            }
        )
        field_order = {f.field_id: i for i, f in enumerate(plan.fields)}
        entity_order = {e.entity_id: i for i, e in enumerate(plan.entities)}
        data["selections"] = tuple(f.selector.canonical_json() for f in plan.fields)
        data["groups"] = tuple(
            (
                e.grouping.canonical_json(),
                tuple(field_order[f] for f in e.field_ids),
                entity_order[e.parent_entity_id] if e.parent_entity_id else None,
            )
            for e in plan.entities
        )
        return canonical_json_value(data)

    async def _finalize(
        self,
        profile: StructureProfile,
        manifest: ExtractedDatasetManifest,
        plan: ParsePlan | None,
        replay: Replay,
        validator: ParsePlanValidator,
        score: SemanticConfidence,
        issues: tuple[ValidationIssue, ...] = (),
        refs: tuple[PhysicalSourceRef, ...] = (),
        unresolved: tuple[PhysicalSourceRef, ...] = (),
        unresolved_blocks: int = 0,
        calls: tuple[LLMCallRecord, ...] = (),
    ) -> HybridAnalysis:
        if any(issue.code == LLMErrorCode.UNSAFE_CONTENT for issue in issues):
            plan = None
            score = assess()
        if (
            plan is not None
            and ParsePlanKind(plan.kind) not in self.policy.structural.allowed_kinds
        ):
            plan = None
            score = assess()
            issues = (*issues, semantic_issue("SEMANTIC_KIND_DENIED"))
        if plan is not None:
            data = plan.model_dump()
            data.update(
                schema_version="1.2.0",
                final_assessment=score,
                confidence=score.confidence,
                fingerprint="sha256:" + "0" * 64,
            )
            plan = type(plan).model_validate(data)
            validation = await validator.validate_source(
                ParsePlanValidationRequest(
                    plan=plan,
                    source=manifest.source,
                    manifest=manifest,
                    profile=profile,
                ),
                open_replay(replay),
            )
            if validation.decision is not ValidationDecision.ACCEPTED:
                plan = None
                issues = (*issues, *validation.issues)
                score = assess()
            elif score.confidence < self.policy.structural.confidence_threshold:
                issues = (*issues, semantic_issue("SEMANTIC_CONFIDENCE_LOW"))
        if plan is None and not issues:
            issues = (semantic_issue("SEMANTIC_NO_ENTITIES"),)
        return HybridAnalysis(
            profile,
            manifest,
            plan,
            score,
            issues[: self.policy.max_issues],
            refs,
            unresolved,
            unresolved_blocks,
            calls,
        )
