"""Детерминированный analyzer с явными исходами и проверенным source scope."""

import asyncio
from collections.abc import AsyncIterable

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.analysis import StructureAnalysisOptions
from structuraguard.contracts.common import (
    IssueSeverity,
    ProducerMetadata,
    SemanticParsingMode,
    ValidationIssue,
)
from structuraguard.contracts.parsing import (
    ParsePlanValidationRequest,
    StructureAnalysisRequest,
    StructureAnalysisResult,
    StructureNeedsReview,
    StructureNeedsSemanticAnalysis,
    StructurePlanCreated,
    StructureProfile,
    StructureRejected,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import StructuralAnalysisError
from structuraguard.structure._stream import limit, preflight
from structuraguard.structure.planning import Planner
from structuraguard.structure.profiling import StructuralProfiler


def _invalid(reason: str) -> StructuralAnalysisError:
    return StructuralAnalysisError(
        error_code="STRUCTURE_INPUT_INVALID",
        message="Некорректный запрос structural analysis",
        details={"reason": reason},
    )


def _issue(code: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity=IssueSeverity.WARNING, message_key=code)


def _needs(profile: StructureProfile, reason: str) -> StructureNeedsSemanticAnalysis:
    return StructureNeedsSemanticAnalysis(profile=profile, issues=(_issue(reason),))


class DeterministicStructureAnalyzer:
    """Строит один проверяемый plan либо сохраняет ranked альтернативы.

    Args:
        options: Неизменяемые sampling budgets, порог confidence и предел plan.

    Raises:
        StructuralAnalysisError: Неверный тип или нарушен контракт options.

    Принимает полный physical stream (или terminal batch), необязательный
    исходный profile; также реализует SemanticStructureAnalyzer protocol.
    Profile/request без physical replay не доказывает координаты и получает
    явный STRUCTURE_REPLAY_REQUIRED. LLM, сеть и исполнение plan отсутствуют.
    """

    def __init__(self, *, options: StructureAnalysisOptions | None = None) -> None:
        if options is not None and type(options) is not StructureAnalysisOptions:
            raise _invalid("options_type")
        try:
            self._options = StructureAnalysisOptions.model_validate(
                (options or StructureAnalysisOptions()).model_dump(mode="python")
            )
        except (ValueError, TypeError, AttributeError):
            raise _invalid("options_contract") from None

    @property
    def options(self) -> StructureAnalysisOptions:
        """Вернуть неизменяемую policy с budgets и порогом confidence."""
        return self._options

    async def analyze(
        self,
        request: StructureAnalysisRequest
        | StructureProfile
        | ExtractedBatch
        | AsyncIterable[ExtractedBatch],
        *,
        batches: ExtractedBatch | AsyncIterable[ExtractedBatch] | None = None,
        profile: StructureProfile | None = None,
    ) -> StructureAnalysisResult:
        """Проверить bounded input, построить кандидатов и закрыть owned stream.

        Args:
            request: Analysis request, исходный profile, terminal batch либо
                полный async stream одного extraction.
            batches: Новый physical replay для request/profile первого аргумента.
            profile: Исходный профиль для проверки stream первого аргумента.

        Returns:
            Discriminated result: plan_created, needs_review,
            needs_semantic_analysis либо rejected. При plan_created результат
            содержит derived profile; именно его передают validator/executor.

        ``batches`` передаёт replay для request/profile; ``profile`` привязывает
        профиль к stream, переданному первым аргументом. Replay обязан получить
        тот же профиль при настроенных sampling options. Confidence — минимум
        evidence components, а не вероятность. Несколько кандидатов требуют review
        независимо от порога; низкий score не вызывает скрытый fallback.

        Raises:
            StructuralAnalysisError: Несогласованные DTO, fingerprints или timeout.
            StructuralProfilingError: Нарушена целостность либо чтение stream.
            ParserError: Очищенная ошибка technical parser при physical replay.
            SecurityPolicyError: Превышен бюджет либо источник отклонён parser.

        Iterator потребляется и закрывается при наличии close-hook; cancellation
        распространяется. Raw values/evidence остаются sensitive данными. LLM,
        network и execution здесь отсутствуют; semantic meaning unresolved.
        """
        try:
            async with asyncio.timeout(self.options.profiling.max_processing_seconds):
                result = await self._analyze(request, batches=batches, profile=profile)
                if (
                    len(result.canonical_json().encode("utf-8"))
                    > self.options.max_plan_bytes
                    + self.options.profiling.max_profile_bytes
                ):
                    raise limit(
                        "analysis_output_bytes",
                        self.options.max_plan_bytes
                        + self.options.profiling.max_profile_bytes,
                    )
                return result
        except TimeoutError:
            raise StructuralAnalysisError(
                error_code="PROCESSING_TIMEOUT",
                message="Истёк deadline structural analysis",
            ) from None

    def _check(
        self, value: StructureProfile | StructureAnalysisRequest
    ) -> StructureProfile | StructureAnalysisRequest:
        if type(value) not in {StructureProfile, StructureAnalysisRequest}:
            raise _invalid("request_type")
        try:
            preflight(value, self.options.profiling)
            return type(value).model_validate(
                value.model_dump(mode="python", warnings="error")
            )
        except (ValueError, TypeError, AttributeError):
            raise _invalid("request_contract") from None

    async def _analyze(
        self,
        request: StructureAnalysisRequest
        | StructureProfile
        | ExtractedBatch
        | AsyncIterable[ExtractedBatch],
        *,
        batches: ExtractedBatch | AsyncIterable[ExtractedBatch] | None,
        profile: StructureProfile | None,
    ) -> StructureAnalysisResult:
        expected = profile
        if isinstance(request, StructureAnalysisRequest | StructureProfile):
            if profile is not None:
                raise _invalid("duplicate_profile")
            checked = self._check(request)
            expected = (
                checked.profile
                if isinstance(checked, StructureAnalysisRequest)
                else checked
            )
            if (
                isinstance(checked, StructureAnalysisRequest)
                and checked.mode is not SemanticParsingMode.DETERMINISTIC
            ):
                return StructureRejected(
                    profile=expected, issues=(_issue("STRUCTURE_MODE_UNSUPPORTED"),)
                )
            if batches is None:
                return _needs(expected, "STRUCTURE_REPLAY_REQUIRED")
            source = batches
        else:
            if batches is not None:
                raise _invalid("duplicate_source")
            source = request
        if not isinstance(source, ExtractedBatch | AsyncIterable):
            raise _invalid("source_type")
        if expected is not None:
            checked_profile = self._check(expected)
            if not isinstance(checked_profile, StructureProfile):
                raise _invalid("profile_type")
            expected = checked_profile
        snapshot = await StructuralProfiler(options=self.options.profiling)._inspect(
            source
        )
        if expected is not None and snapshot.profile != expected:
            raise _invalid("profile_replay_mismatch")
        planner = Planner(snapshot, self.options)
        drafts = planner.build()
        await asyncio.sleep(0)
        if not drafts:
            return _needs(snapshot.profile, "STRUCTURE_RULES_INSUFFICIENT")
        payload = snapshot.profile.model_dump(mode="python")
        payload.update(
            candidates=tuple(d.candidate for d in drafts),
            profile_fingerprint="sha256:" + "0" * 64,
            profile_id="analysis_"
            + canonical_sha256_value(
                (snapshot.profile.profile_fingerprint, self.options.canonical_json())
            )[-24:],
            producer=ProducerMetadata(
                component_id="deterministic_structure_analyzer",
                component_version="1.0.0",
                sdk_version="0.3.0",
            ),
        )
        analyzed_profile = StructureProfile.model_validate(payload)
        if len(drafts) > 1:
            return StructureNeedsReview(
                profile=analyzed_profile,
                candidates=analyzed_profile.candidates,
                issues=(
                    _issue("STRUCTURE_MULTIPLE_CANDIDATES"),
                    *(
                        (_issue("STRUCTURE_UNHANDLED_SCOPE"),)
                        if planner.unhandled
                        else ()
                    ),
                    *(
                        (_issue("STRUCTURE_CANDIDATE_LIMIT"),)
                        if planner.overflow
                        else ()
                    ),
                ),
            )
        draft = drafts[0]
        if planner.overflow:
            return _needs(analyzed_profile, "STRUCTURE_CANDIDATE_LIMIT")
        if planner.unhandled:
            return _needs(analyzed_profile, "STRUCTURE_UNHANDLED_SCOPE")
        if (
            draft.plan is None
            or draft.candidate.confidence < self.options.confidence_threshold
        ):
            return _needs(
                analyzed_profile,
                "STRUCTURE_RULES_INSUFFICIENT"
                if draft.plan is None
                else "STRUCTURE_CONFIDENCE_LOW",
            )
        plan_payload = draft.plan.model_dump(mode="python")
        plan_payload.update(
            profile_fingerprint=analyzed_profile.profile_fingerprint,
            fingerprint="sha256:" + "0" * 64,
        )
        plan = type(draft.plan).model_validate(plan_payload)
        if len(plan.canonical_json().encode("utf-8")) > self.options.max_plan_bytes:
            raise limit("analysis_plan_bytes", self.options.max_plan_bytes)
        # Membership — дополнение к компиляции только из проверенных samples.
        # Это не capability и не замена независимому validator/executor M5-C.
        ParsePlanValidationRequest(
            plan=plan,
            source=snapshot.manifest.source,
            manifest=snapshot.manifest,
            profile=analyzed_profile,
        )
        return StructurePlanCreated(profile=analyzed_profile, plan=plan)
