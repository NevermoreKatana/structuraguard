"""Run-scoped semantic mapper поверх M9 и существующего M6 router."""

import asyncio

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import PipelineStatus
from structuraguard.contracts.database import CatalogColumnRef, DatabaseCatalog
from structuraguard.contracts.deterministic_mapping import (
    DeterministicMappingOptions,
    MappingScope,
)
from structuraguard.contracts.llm import LLMErrorCode, LLMRoutingMode
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.reports import (
    LLMRequest,
    SecurityApproval,
    SecurityReport,
    SecurityScanRequest,
)
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog
from structuraguard.contracts.semantic_mapping import (
    SemanticGroupResult,
    SemanticMappingContext,
    SemanticMappingOptions,
    SemanticMappingResult,
)
from structuraguard.exceptions import LLMProviderError, MappingError
from structuraguard.llm import PolicyAwareLLMRouter
from structuraguard.ports.security import SecurityScanner
from structuraguard.profiling.pii import maximum_classification

from ._inputs import bounded_size, failure
from ._semantic_candidates import (
    check_deadline,
    checked_options,
    prepare_semantic_mapping,
)
from ._semantic_confidence import aggregate, proposal_action
from ._semantic_prompt import semantic_mapping_prompt, semantic_mapping_response_schema
from ._semantic_validation import validate_decision


class LLMSemanticMapper:
    """Предлагать mappings через M9 и один внедрённый M6 router на весь run.

    Args:
        router: Координатор M6 с общими budgets, routing policy и fallback.
        scanner: Доверенный SecurityScanner для точного payload и PII policy.
        context: Run identity router и минимальные классы данных/metadata.
        options: Бюджеты, пороги, штрафы и retention M10; None включает defaults.
        ranking_options: Параметры M9; column top-k дополнительно ограничен M10.

    Конструктор не выполняет I/O. Provider lifecycle принадлежит приложению.
    Одновременные propose на одном instance запрещены. Методы не исполняют SQL,
    MappingPlan или DB writes; передаваемые source/metadata всегда недоверенные.

    Raises:
        MappingError: Некорректные options или превышение preflight budget.
        pydantic.ValidationError: Некорректный context; ошибка может содержать
            входные значения и не предназначена для обычного log.
    """

    def __init__(
        self,
        *,
        router: PolicyAwareLLMRouter,
        scanner: SecurityScanner,
        context: SemanticMappingContext,
        options: SemanticMappingOptions | None = None,
        ranking_options: DeterministicMappingOptions | None = None,
    ) -> None:
        self._options = checked_options(options)
        bounded_size(context, 65536)
        self._context = SemanticMappingContext.model_validate(
            context.model_dump(warnings="error")
        )
        self._router, self._scanner = router, scanner
        self._ranking_options = ranking_options
        self._busy = False

    async def propose(
        self,
        profile: NormalizedDataProfile,
        catalog: DatabaseCatalog,
        *,
        scope: MappingScope,
        semantic_catalog: DatabaseSemanticCatalog | None = None,
    ) -> SemanticMappingResult:
        """Вернуть предложение mappings с SDK scores и происхождением ответа M6.

        Args:
            profile: Завершённый NormalizedDataProfile M8 schema 1.0.0.
            catalog: DatabaseCatalog M7 schema 1.1.0 / catalog-v1.
            scope: Явный allow/deny scope, привязанный к target и его policy.
            semantic_catalog: Необязательные aliases/descriptions с теми же bindings.

        Returns:
            Чувствительный SemanticMappingResult. COMPLETED/auto относятся только
            к proposal; confirm/reject сохраняют NEEDS_REVIEW. Для logs используйте
            safe_summary(), в том числе при metadata_only retention.

        Активные группы проходят scanner и router, расходуют общий бюджет и могут
        обращаться к разрешённому provider. При no_llm или пустых candidates
        scan/generation отсутствуют. В LLM передаётся только masked проекция;
        raw examples, полный catalog, credentials, tools и SQL запрещены.

        Raises:
            MappingError: Неверные входы/лимиты, неизвестный ID или несогласованный
                split/FK; частичный результат не возвращается.
            DatabaseInspectionError: DATABASE_SCHEMA_DRIFT при несогласованном
                catalog fingerprint; свежесть живой БД здесь не проверяется.
            LLMProviderError: M6 typed failure, запрет scan/egress, malformed output
                либо общий timeout. Конкурентный вызов даёт LLM_BUDGET_EXCEEDED;
                history фактических attempts доступна в переданном router.
            asyncio.CancelledError: Отмена без retry и частичного результата.
        """
        if self._busy:
            raise LLMProviderError(LLMErrorCode.BUDGET_EXCEEDED)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._options.max_seconds
        self._busy = True
        try:
            async with asyncio.timeout_at(deadline):
                result = await self._propose(
                    profile, catalog, scope, semantic_catalog, deadline
                )
                check_deadline(deadline)
                return result
        except MappingError:
            # Вложенная preparation имеет свой typed deadline; общий run уже истёк.
            if loop.time() >= deadline:
                raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
            raise
        except TimeoutError:
            raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
        finally:
            self._busy = False

    async def _approved_request(self, scan: SecurityScanRequest) -> LLMRequest:
        try:
            report = await self._scanner.scan(scan)
        except TimeoutError:
            raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
        except asyncio.CancelledError:
            raise asyncio.CancelledError from None
        except BaseException as error:
            # Только внешний adapter: его exception text/notes могут содержать PII.
            # Как в M6, process-control исключения сохраняются без преобразования.
            if not isinstance(error, Exception):
                raise
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None
        try:
            bounded_size(report, 65536)
            checked = SecurityReport.model_validate(report.model_dump(warnings="error"))
            if checked.request_id != scan.request_id:
                raise ValueError("Security report относится к другому scan request")
            approval = SecurityApproval(
                report=checked, report_fingerprint=canonical_sha256_value(checked)
            )
            return LLMRequest(
                request_id=scan.request_id,
                run_id=scan.run_id,
                purpose="semantic_mapping",
                response_schema_id="semantic-mapping-decision",
                response_schema_version="1.0.0",
                payload_json=scan.payload_json,
                payload_fingerprint=scan.payload_fingerprint,
                content_fingerprint=scan.content_fingerprint,
                data_classification=scan.data_classification,
                routing_policy_id=scan.routing_policy_id,
                routing_policy_fingerprint=scan.routing_policy_fingerprint,
                redaction_fingerprint=scan.redaction_fingerprint,
                security_approval=approval,
                prompt_fingerprint=semantic_mapping_prompt().identity.fingerprint,
                prompt=semantic_mapping_prompt().identity,
                max_output_bytes=self._options.max_response_bytes,
            )
        except TimeoutError:
            raise LLMProviderError(LLMErrorCode.TIMEOUT) from None
        except (
            ValueError,
            TypeError,
            AttributeError,
            RecursionError,
            RuntimeError,
            OSError,
        ):
            raise LLMProviderError(LLMErrorCode.POLICY_DENIED) from None

    async def _propose(
        self,
        profile: NormalizedDataProfile,
        catalog: DatabaseCatalog,
        scope: MappingScope,
        semantic_catalog: DatabaseSemanticCatalog | None,
        deadline: float,
    ) -> SemanticMappingResult:
        from decimal import Decimal
        from hashlib import sha256

        prepared = await prepare_semantic_mapping(
            profile,
            catalog,
            scope=scope,
            semantic_catalog=semantic_catalog,
            options=self._options,
            ranking_options=self._ranking_options,
        )
        classification = maximum_classification(
            prepared.classification,
            self._context.data_classification,
            self._context.metadata_classification,
        )
        groups = []
        selected_targets: list[CatalogColumnRef] = []
        # Registry validation относится к trusted schema, а не к данным модели.
        schema = semantic_mapping_response_schema()
        for group in prepared.groups:
            check_deadline(deadline)
            if self._router.policy.mode is LLMRoutingMode.NO_LLM or not group.columns:
                groups.append(
                    SemanticGroupResult(
                        candidates=group,
                        decision=None,
                        choices=(),
                        confidence=Decimal(0),
                        ambiguous=any(f.ranked.ambiguous for f in group.fields),
                        reasons=(
                            "LLM_DISABLED"
                            if self._router.policy.mode is LLMRoutingMode.NO_LLM
                            else "NO_ADMISSIBLE_CANDIDATES",
                        ),
                        prompt=semantic_mapping_prompt().identity,
                        calls=(),
                        action="confirm"
                        if self._router.policy.mode is LLMRoutingMode.NO_LLM
                        else "reject",
                    )
                )
                continue
            payload_hash = "sha256:" + sha256(group.payload_json.encode()).hexdigest()
            scan = SecurityScanRequest(
                request_id="semantic_" + group.group_id + "_" + payload_hash[-16:],
                run_id=self._context.run_id,
                purpose="llm_input",
                content_fingerprint=group.columns[0].mapping.source_fingerprint,
                payload_json=group.payload_json,
                payload_fingerprint=payload_hash,
                data_classification=classification,
                routing_policy_id=self._router.policy.policy_id,
                routing_policy_fingerprint=self._router.policy_fingerprint,
                redaction_fingerprint=canonical_sha256_value(
                    {"version": "semantic_masking_v2", "payload": payload_hash}
                ),
            )
            check_deadline(deadline)
            request = await self._approved_request(scan)
            start = len(self._router.calls)
            check_deadline(deadline)
            response = await self._router.generate_structured(request)
            check_deadline(deadline)
            decision = validate_decision(response.output_json, group, self._options)
            check_deadline(deadline)
            choices, confidence, ambiguous, reasons = aggregate(
                group,
                decision,
                self._options,
                security_warning=request.security_approval.report.status
                is PipelineStatus.COMPLETED_WITH_WARNINGS,
            )
            # Lookup разрешён только после schema/membership/FK validation, до retention.
            selected = {c.selected_candidate_id for c in decision.columns}
            selected_targets.extend(
                c.mapping.target for c in group.columns if c.candidate_id in selected
            )
            groups.append(
                SemanticGroupResult(
                    candidates=group,
                    decision=decision
                    if self._options.response_retention == "validated_decision"
                    else None,
                    choices=choices,
                    confidence=confidence,
                    ambiguous=ambiguous,
                    reasons=reasons,
                    prompt=semantic_mapping_prompt().identity,
                    calls=self._router.calls[start:],
                    action=proposal_action(tuple(c.action for c in choices), reasons),
                    security_report_fingerprint=request.security_approval.report_fingerprint,
                )
            )
        if len(set(selected_targets)) != len(selected_targets):
            raise failure(
                "SEMANTIC_MAPPING_DECISION_INVALID", "cross_group_target_collision"
            )
        result = SemanticMappingResult(
            deterministic=prepared.deterministic,
            groups=tuple(groups),
            status=PipelineStatus.NEEDS_REVIEW
            if not groups or any(g.action != "auto" for g in groups)
            else PipelineStatus.COMPLETED,
            classification=classification,
            routing_policy_fingerprint=self._router.policy_fingerprint,
            options_fingerprint=canonical_sha256_value(self._options),
            action=proposal_action(tuple(g.action for g in groups), ()),
            response_retention=self._options.response_retention,
            response_schema_id=schema.schema_id,
            response_schema_version=schema.version,
            response_schema_fingerprint=schema.fingerprint,
        )
        bounded_size(result, self._options.max_state_bytes)
        return result
