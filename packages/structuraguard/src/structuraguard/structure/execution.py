"""Streaming ParsePlan execution с runtime verification и terminal-only commit."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import ProducerMetadata
from structuraguard.contracts.document_semantics import DocumentSpanSelector
from structuraguard.contracts.execution import (
    ExecutionStage,
    ParsePlanOptions,
    SelectionOperation,
    SelectionTrace,
)
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedBatchSummary,
    NormalizedDatasetManifest,
    NormalizedRecord,
    NormalizedValue,
    SemanticEntity,
    SemanticField,
    SemanticSourceIndex,
)
from structuraguard.contracts.parsing import (
    ParseExecutionContext,
    ParsePlanValidationRequest,
    ValidatedParsePlan,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import ParseExecutionError
from structuraguard.structure._plan_check import (
    bounded_payload,
    failure,
    prepare,
    validation_fingerprint,
)
from structuraguard.structure._runtime import PlanRuntime, SelectedRecord
from structuraguard.structure.validation import (
    ParsePlanValidator,
    close_source,
    next_source,
    source_iterator,
)


class _Output:
    def __init__(
        self,
        context: ParseExecutionContext,
        checked: ParsePlanValidationRequest,
        options: ParsePlanOptions,
    ) -> None:
        self.context = context
        self.checked = checked
        self.options = options
        self.summaries: list[NormalizedBatchSummary] = []
        self.pending: list[NormalizedRecord] = []
        self.pending_bytes = 0
        self.records = 0
        self.prefix = canonical_sha256_value(
            (context.run_id, checked.plan.fingerprint)
        )[-20:]
        self.producer = ProducerMetadata(
            component_id="parse_plan_executor",
            component_version="1.0.0",
            sdk_version="0.3.0",
        )
        self.schema_version = (
            "1.2.0"
            if any(
                isinstance(f.selector, DocumentSpanSelector)
                for f in checked.plan.fields
            )
            else "1.1.0"
        )

    def normalized(self, selected: SelectedRecord) -> NormalizedRecord:
        record_id = f"record_{self.prefix}_{self.records}"
        self.records += 1
        entities: list[SemanticEntity] = []
        for index, entity in enumerate(selected.entities):
            entity_id = f"entity_{self.prefix}_{self.records}_{index}"
            values = tuple(
                NormalizedValue(
                    value_id=f"value_{self.prefix}_{self.records}_{index}_{field_index}",
                    field_name=value.field.semantic_name,
                    raw_value=value.raw,
                    normalized_value=value.normalized,
                    semantic_type=value.field.semantic_type,
                    source_refs=tuple(
                        dict.fromkeys(o.source_ref for o in value.origins)
                    ),
                    origins=value.origins,
                    selection=SelectionTrace(
                        operation=value.operation,
                        selector_fingerprint=canonical_sha256_value(
                            value.field.selector
                        ),
                    ),
                    transformations=()
                    if value.operation is SelectionOperation.COPY
                    else (value.operation.value,),
                )
                for field_index, value in enumerate(entity.values)
            )
            entities.append(
                SemanticEntity(
                    entity_id=entity_id,
                    entity_type=entity.definition.entity_type,
                    values=values,
                    source_refs=tuple(
                        dict.fromkeys(
                            ref for value in values for ref in value.source_refs
                        )
                    ),
                    parent_entity_id=entities[entity.parent].entity_id
                    if entity.parent is not None
                    else None,
                )
            )
        return NormalizedRecord(
            record_id=record_id,
            entities=tuple(entities),
            source_refs=tuple(
                dict.fromkeys(ref for entity in entities for ref in entity.source_refs)
            ),
        )

    def append(self, record: SelectedRecord) -> list[NormalizedBatch]:
        normalized = self.normalized(record)
        size = len(normalized.canonical_json().encode("utf-8"))
        if size > self.options.max_output_batch_bytes:
            raise failure(
                "output_record_bytes",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )
        output = []
        if (
            self.pending
            and self.pending_bytes + size > self.options.max_output_batch_bytes
        ):
            output.append(self.batch())
        self.pending.append(normalized)
        self.pending_bytes += size
        if len(self.pending) >= self.context.max_records_per_batch:
            output.append(self.batch())
        return output

    def batch(self, *, terminal: bool = False) -> NormalizedBatch:
        if len(self.summaries) >= self.options.max_output_batches:
            raise failure(
                "output_batch_limit",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )
        prototype = NormalizedBatch(
            schema_version=self.schema_version,
            source=self.checked.source,
            extraction_id=self.checked.manifest.extraction_id,
            extraction_fingerprint=self.checked.manifest.extraction_fingerprint,
            parse_plan_fingerprint=self.checked.plan.fingerprint,
            producer=self.producer,
            batch_index=len(self.summaries),
            batch_fingerprint="sha256:" + "0" * 64,
            records=tuple(self.pending),
        )
        self.pending = []
        self.pending_bytes = 0
        if not terminal:
            self.summaries.append(prototype.to_summary())
            self._batch_limit(prototype)
            return prototype
        payload = prototype.model_dump(mode="python")
        payload["is_last"] = True
        fingerprint = canonical_sha256_value(
            payload, exclude_top_level=frozenset({"batch_fingerprint", "manifest"})
        )
        summary = prototype.to_summary().model_copy(
            update={"batch_fingerprint": fingerprint}
        )
        self.summaries.append(summary)
        fields = {f.field_id: f for f in self.checked.plan.fields}
        schema = tuple(
            dict.fromkeys(
                SemanticField(
                    entity_type=entity.entity_type,
                    field_name=fields[field_id].semantic_name,
                    semantic_type=fields[field_id].semantic_type,
                )
                for entity in self.checked.plan.entities
                for field_id in entity.field_ids
            )
        )
        manifest = NormalizedDatasetManifest(
            schema_version=self.schema_version,
            source=self.checked.source,
            extraction_id=self.checked.manifest.extraction_id,
            extraction_fingerprint=self.checked.manifest.extraction_fingerprint,
            parse_plan_fingerprint=self.checked.plan.fingerprint,
            producer=self.producer,
            batches=tuple(self.summaries),
            normalized_fingerprint="sha256:" + "0" * 64,
            record_count=sum(s.record_count for s in self.summaries),
            entity_count=sum(s.entity_count for s in self.summaries),
            value_count=sum(s.value_count for s in self.summaries),
            semantic_schema=schema,
            semantic_fields=tuple(dict.fromkeys(f.field_name for f in schema)),
            semantic_index=SemanticSourceIndex(fields=tuple(f.ref for f in schema)),
        )
        payload.update(batch_fingerprint=fingerprint, manifest=manifest)
        terminal_batch = NormalizedBatch.model_validate(payload)
        self._batch_limit(terminal_batch)
        return terminal_batch

    def _batch_limit(self, batch: NormalizedBatch) -> None:
        if (
            len(batch.canonical_json().encode("utf-8"))
            > self.options.max_output_batch_bytes
        ):
            raise failure(
                "output_batch_bytes",
                code="SECURITY_LIMIT_EXCEEDED",
                stage=ExecutionStage.LIMIT,
            )


class ParsePlanExecutor:
    """Применяет только закрытую policy к повторно проверяемому source stream.

    Args:
        options: Те же budgets/policy, с которыми validator принял plan.

    Raises:
        ParseExecutionError: Передан неверный тип options (PARSE_PLAN_INVALID).
        ValueError: Нарушен контракт значений options.

    ``context.profile`` обязателен. Каждый yield до terminal manifest является
    промежуточным: downstream обязан staging/rollback при fatal issue. Executor
    не вызывает LLM, сеть и conversions; raw values и physical origins сохранены.
    """

    def __init__(self, *, options: ParsePlanOptions | None = None) -> None:
        self.options = ParsePlanValidator(options=options).options

    async def execute(
        self,
        batches: AsyncIterable[ExtractedBatch],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
    ) -> AsyncGenerator[NormalizedBatch, None]:
        """Прочитать/закрыть iterator; выдать bounded batches либо ParseExecutionError.

        Args:
            batches: Новый полный async replay проверенного extraction.
            plan: Сериализуемый wrapper validator; повторно проверяется при чтении.
            context: Run ID, fingerprints, manifest, тот же обязательный profile,
                что у validator, и records на output batch (не более 1000).

        Yields:
            NormalizedBatch schema 1.1.0 с raw values и physical provenance.
            Только terminal manifest после EOF и cleanup подтверждает успех.

        Raises:
            ParseExecutionError: Ошибка plan/source/selection/limit/timeout/cleanup;
                issue содержит безопасные code, stage, reason и emitted_batches.
            asyncio.CancelledError: Отмена после cleanup, без успешного terminal.

        Ошибки возникают при итерации. Consumer закрывает generator через aclose()
        при раннем выходе и откатывает предварительный output при поздней ошибке.
        Staging и DB writes выполняет caller. Raw values могут содержать PII;
        LLM, сеть, исполняемый regex и callbacks из plan не используются.
        Cancellation распространяется. Deadline учитывает только активную работу,
        поэтому между yield нет timer, который мог бы отменить idle consumer.
        """
        loop = asyncio.get_running_loop()
        validation_started = loop.time()
        iterator: AsyncIterator[ExtractedBatch] | None = None
        primary: BaseException | None = None
        closed = False
        emitted = 0
        try:
            iterator = source_iterator(batches)
            try:
                bounded_payload(plan, self.options)
                bounded_payload(context, self.options)
                checked_plan = ValidatedParsePlan.model_validate(
                    plan.model_dump(mode="python", warnings="error")
                )
                checked_context = ParseExecutionContext.model_validate(
                    context.model_dump(mode="python", warnings="error")
                )
                if checked_context.profile is None:
                    raise ValueError("profile_required")
                request = ParsePlanValidationRequest(
                    plan=checked_plan.plan,
                    source=checked_context.manifest.source,
                    manifest=checked_context.manifest,
                    profile=checked_context.profile,
                )
                request = prepare(request, self.options)
                if (
                    checked_context.parse_plan_fingerprint != request.plan.fingerprint
                    or checked_plan.validator_id != "parse_plan_validator"
                    or checked_plan.validator_version != "1.0.0"
                    or checked_plan.validation_fingerprint
                    != validation_fingerprint(request, self.options)
                ):
                    raise ValueError("validation_binding")
                if checked_context.max_records_per_batch > 1_000:
                    raise ValueError("output_batch_records")
            except (ValueError, TypeError, AttributeError):
                raise failure(
                    "execution_contract",
                    code="PARSE_PLAN_INVALID",
                    stage=ExecutionStage.VALIDATION,
                ) from None
            runtime = PlanRuntime(request, self.options)
            output = _Output(checked_context, request, self.options)
            remaining = float(self.options.source_limits.max_processing_seconds) - (
                loop.time() - validation_started
            )
            if remaining <= 0:
                raise TimeoutError
            started = loop.time()
            while True:
                try:
                    async with asyncio.timeout(remaining):
                        batch = await next_source(iterator)
                except StopAsyncIteration:
                    break
                for record in runtime.consume(batch):
                    for result in output.append(record):
                        remaining -= loop.time() - started
                        if remaining <= 0:
                            raise TimeoutError
                        emitted += 1
                        yield result
                        started = loop.time()
                    await asyncio.sleep(0)
                remaining -= loop.time() - started
                if remaining <= 0:
                    raise TimeoutError
                started = loop.time()
                await asyncio.sleep(0)
            for record in runtime.finish():
                for result in output.append(record):
                    remaining -= loop.time() - started
                    if remaining <= 0:
                        raise TimeoutError
                    emitted += 1
                    yield result
                    started = loop.time()
            closed = True
            await close_source(iterator)
            if loop.time() - started > remaining:
                raise TimeoutError
            yield output.batch(terminal=True)
        except ParseExecutionError as error:
            primary = error
            # Counters относятся к этому run; raw exception input не переносится.
            if error.issue.emitted_batches != emitted:
                primary = ParseExecutionError(
                    error.issue.model_copy(update={"emitted_batches": emitted})
                )
            raise primary from None
        except TimeoutError:
            primary = failure(
                "execution_deadline",
                code="PROCESSING_TIMEOUT",
                stage=ExecutionStage.TIMEOUT,
                emitted=emitted,
            )
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            if iterator is not None and not closed:
                await close_source(iterator, primary)
