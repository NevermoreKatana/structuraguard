"""Проверка physical evidence через существующий bounded ParsePlan executor."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import IssueSeverity, PhysicalSourceRef
from structuraguard.contracts.execution import ParsePlanOptions
from structuraguard.contracts.normalization import NormalizationResult
from structuraguard.contracts.normalized import (
    NormalizedBatch,
    NormalizedRecord,
    NormalizedValue,
    SemanticFieldRef,
)
from structuraguard.contracts.parsing import ParseExecutionContext, ValidatedParsePlan
from structuraguard.contracts.profiling import NormalizedProfilingOptions
from structuraguard.contracts.provenance import (
    ArtifactValueReference,
    DetailedValidationReport,
    ProvenanceLimits,
    ProvenancePolicy,
    SourceEvidenceLocation,
    ValidationFinding,
    ValidationLayer,
    ValidationLineage,
    ValueProvenanceEvidence,
)
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.exceptions import (
    NormalizedProfilingError,
    ParseExecutionError,
    ValidationError,
)
from structuraguard.normalization import NormalizerRegistrySnapshot
from structuraguard.profiling._stream import Ledger, StreamCheck
from structuraguard.validation._bounded import BoundedInput, failure
from structuraguard.validation.reporting import build_report


@dataclass(frozen=True, slots=True)
class _Value:
    record: int
    entity: int
    index: int
    field: SemanticFieldRef
    value: NormalizedValue
    batch_fingerprint: str
    pointer: str


class _Findings:
    def __init__(self, limits: ProvenanceLimits) -> None:
        self.limit = limits.max_issues
        self.items: list[ValidationFinding] = []

    def add(
        self,
        code: str,
        value: _Value | None = None,
        *,
        record: int | None = None,
        layer: ValidationLayer = ValidationLayer.PROVENANCE,
        check: int = 0,
        unverified: bool = False,
    ) -> None:
        if len(self.items) >= self.limit:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        self.items.append(
            ValidationFinding(
                layer=layer,
                code=code,
                record_index=value.record if value else record,
                entity_index=value.entity if value else None,
                value_index=value.index if value else None,
                check_index=check,
                severity=IssueSeverity.WARNING if unverified else IssueSeverity.ERROR,
                outcome="unverified" if unverified else "failed",
            )
        )


async def _stream(batches: tuple[ExtractedBatch, ...]) -> AsyncIterator[ExtractedBatch]:
    for batch in batches:
        yield batch


def _values(batches: tuple[NormalizedBatch, ...]) -> tuple[_Value, ...]:
    result: list[_Value] = []
    ordinal = 0
    for batch in batches:
        for local, record in enumerate(batch.records):
            for ei, entity in enumerate(record.entities):
                for vi, value in enumerate(entity.values):
                    result.append(
                        _Value(
                            ordinal,
                            ei,
                            vi,
                            SemanticFieldRef(
                                entity_type=entity.entity_type,
                                field_name=value.field_name,
                            ),
                            value,
                            batch.batch_fingerprint,
                            f"/records/{local}/entities/{ei}/values/{vi}",
                        )
                    )
            ordinal += 1
    return tuple(result)


def _compare_value(
    item: _Value,
    expected: NormalizedValue,
    refs: set[PhysicalSourceRef],
    policy: ProvenancePolicy,
    findings: _Findings,
) -> bool:
    value = item.value
    before = len(findings.items)
    required = (
        policy.require_null
        if value.raw_value.kind == "null"
        else policy.require_non_null
    )
    if any(ref not in refs for ref in value.source_refs):
        findings.add("PROVENANCE_REF_UNKNOWN", item, check=1)
    elif value.source_refs != expected.source_refs:
        findings.add("PROVENANCE_SELECTION_MISMATCH", item, check=2)
    if value.raw_value.model_dump_json() != expected.raw_value.model_dump_json():
        findings.add("PROVENANCE_RAW_MISMATCH", item, check=3)
    if (
        value.normalized_value.model_dump_json()
        != expected.normalized_value.model_dump_json()
    ):
        findings.add("PROVENANCE_SELECTION_MISMATCH", item, check=4)
    if (value.value_id, value.field_name, value.semantic_type, value.issue_codes) != (
        expected.value_id,
        expected.field_name,
        expected.semantic_type,
        expected.issue_codes,
    ):
        findings.add("PROVENANCE_STRUCTURE_MISMATCH", item, check=5)
    if not value.origins:
        if value.transformations:
            findings.add("PROVENANCE_SELECTION_MISMATCH", item, check=7)
    else:
        if tuple(
            (o.source_ref, o.location, o.source_spans) for o in value.origins
        ) != tuple(
            (o.source_ref, o.location, o.source_spans) for o in expected.origins
        ):
            findings.add("PROVENANCE_LOCATION_MISMATCH", item, check=6)
        if tuple(o.raw_value.model_dump_json() for o in value.origins) != tuple(
            o.raw_value.model_dump_json() for o in expected.origins
        ):
            findings.add("PROVENANCE_RAW_MISMATCH", item, check=7)
        if (value.selection, value.transformations) != (
            expected.selection,
            expected.transformations,
        ):
            findings.add("PROVENANCE_SELECTION_MISMATCH", item, check=8)
    return len(findings.items) == before and not (required and not value.origins)


def _compare_record(
    index: int,
    actual: NormalizedRecord,
    expected: NormalizedRecord,
    values: dict[tuple[int, int, int], _Value],
    refs: set[PhysicalSourceRef],
    policy: ProvenancePolicy,
    findings: _Findings,
) -> set[str]:
    verified: set[str] = set()
    structure_valid = (
        actual.record_id,
        actual.source_refs,
        actual.parent_record_id,
        actual.related_record_ids,
    ) == (
        expected.record_id,
        expected.source_refs,
        expected.parent_record_id,
        expected.related_record_ids,
    ) and len(actual.entities) == len(expected.entities)
    if not structure_valid:
        findings.add("PROVENANCE_STRUCTURE_MISMATCH", record=index)
    for ei, entity in enumerate(actual.entities):
        if ei >= len(expected.entities):
            continue
        other = expected.entities[ei]
        entity_valid = (
            entity.entity_id,
            entity.entity_type,
            entity.parent_entity_id,
            entity.source_refs,
        ) == (
            other.entity_id,
            other.entity_type,
            other.parent_entity_id,
            other.source_refs,
        ) and len(entity.values) == len(other.values)
        if not entity_valid:
            findings.add("PROVENANCE_STRUCTURE_MISMATCH", record=index, check=ei + 1)
        for vi, value in enumerate(entity.values):
            if vi >= len(other.values):
                continue
            accepted = _compare_value(
                values[index, ei, vi], other.values[vi], refs, policy, findings
            )
            if accepted and entity_valid and structure_valid:
                verified.add(value.value_id)
    return verified


class ProvenanceValidator:
    """Проверить bounded завершённые snapshots без открытия locations или I/O.

    Args:
        policy: Required evidence, normalization bindings и budgets владельца.
        registry: Frozen registrations для повторения разрешённых normalizations;
            обязателен при наличии normalization bindings в policy.
        parse_options: Ограничения M5 executor; None выбирает defaults.

    Raises:
        ValidationError: Malformed config, budget либо отсутствующий/невалидный
            registry (NORMALIZATION_REGISTRY_REQUIRED / NORMALIZATION_REGISTRY_INVALID).

    Source/context/plan и registry задаёт доверенный composition owner. Hash не
    удостоверяет источник сам по себе: executor проверяет physical snapshot и
    выбранные значения. Исходные bytes и полномочия владельца вне этой границы.
    Malformed DTO/лимит дают безопасный ValidationError; проверяемые расхождения
    собираются в report. Cancellation распространяется без успешного report.
    """

    def __init__(
        self,
        *,
        policy: ProvenancePolicy | None = None,
        registry: NormalizerRegistrySnapshot | None = None,
        parse_options: ParsePlanOptions | None = None,
    ) -> None:
        boundary = BoundedInput(ProvenanceLimits())
        self._policy = boundary.checked(
            policy if policy is not None else ProvenancePolicy(), ProvenancePolicy
        )
        if registry is not None and type(registry) is not NormalizerRegistrySnapshot:
            raise failure("NORMALIZATION_REGISTRY_INVALID")
        if self._policy.normalizations and registry is None:
            raise failure("NORMALIZATION_REGISTRY_REQUIRED")
        self._registry = registry
        self._parse_options = boundary.checked(
            parse_options if parse_options is not None else ParsePlanOptions(),
            ParsePlanOptions,
        )

    async def validate(
        self,
        batches: tuple[NormalizedBatch, ...],
        *,
        source_batches: tuple[ExtractedBatch, ...],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
        generated_at: datetime,
        normalizations: tuple[NormalizationResult, ...] = (),
    ) -> DetailedValidationReport:
        """Повторить selection/normalization и вернуть итог для этих двух уровней.

        Args:
            batches: Полный immutable snapshot NormalizedBatch после EOF.
            source_batches: Physical ExtractedBatch того же source fingerprint.
            plan: Проверенный ParsePlan, выбранный доверенным владельцем.
            context: Исходный контекст M5 исполнения, включая run/source identity.
            generated_at: Явное время отчёта с timezone UTC.
            normalizations: Sidecars для bindings из policy; исходные DTO сохраняются.

        Returns:
            DetailedValidationReport с evidence, всеми findings и безопасной сводкой.
            ACCEPTED относится только к объявленным уровням и не разрешает загрузку.

        Raises:
            ValidationError: Невалидный DTO/тип (PROVENANCE_INPUT_INVALID),
                нарушение budget (SECURITY_LIMIT_EXCEEDED) или protocol normalizer.

        Replay использует переданные snapshots без I/O и не исправляет provenance.
        Полный report чувствителен; для обычных logs предназначен safe_summary().
        Ordinals не зависят от normalized batch cuts. generated_at задаётся явно
        и исключён из evidence hash. Неполный physical EOF даёт failed report;
        malformed normalized DTO отклоняется до чтения его полей/serialization.
        """
        limits = self._policy.limits
        boundary = BoundedInput(limits)
        if (
            type(batches) is not tuple
            or type(source_batches) is not tuple
            or type(normalizations) is not tuple
        ):
            raise failure()
        if (
            not batches
            or len(batches) > limits.max_batches
            or len(source_batches) > limits.max_batches
            or len(normalizations) > limits.max_values
        ):
            raise failure("SECURITY_LIMIT_EXCEEDED")
        if type(generated_at) is not datetime or generated_at.tzinfo is not UTC:
            raise failure()
        boundary.scan(generated_at)
        checked_context = boundary.checked(context, ParseExecutionContext)
        checked_plan = boundary.checked(plan, ValidatedParsePlan)
        physical = tuple(
            boundary.checked(batch, ExtractedBatch) for batch in source_batches
        )
        normalized = tuple(
            boundary.checked(batch, NormalizedBatch) for batch in batches
        )
        traces = tuple(
            boundary.checked(item, NormalizationResult) for item in normalizations
        )
        records = tuple(record for batch in normalized for record in batch.records)
        if len(records) > limits.max_records:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        values = _values(normalized)
        if len(values) > limits.max_values:
            raise failure("SECURITY_LIMIT_EXCEEDED")
        findings = _Findings(limits)
        for item in values:
            value_required = (
                self._policy.require_null
                if item.value.raw_value.kind == "null"
                else self._policy.require_non_null
            )
            if value_required and not item.value.origins:
                findings.add("PROVENANCE_REQUIRED", item, check=6, unverified=True)
        options = NormalizedProfilingOptions()
        stream = StreamCheck(options, Ledger(options.max_state_bytes))
        stream_valid = True
        try:
            for batch in normalized:
                stream.accept(batch)
            stream.finish()
        except NormalizedProfilingError:
            stream_valid = False
            findings.add("PROVENANCE_STREAM_INVALID")
        expected_source = checked_context.manifest.source
        source_valid = all(
            batch.source == expected_source for batch in physical
        ) and all(batch.source == expected_source for batch in normalized)
        lineage_valid = all(
            batch.extraction_id == checked_context.manifest.extraction_id
            and batch.extraction_fingerprint == checked_context.extraction_fingerprint
            and batch.parse_plan_fingerprint == checked_context.parse_plan_fingerprint
            for batch in normalized
        )
        if not source_valid:
            findings.add("PROVENANCE_SOURCE_MISMATCH")
        if not lineage_valid:
            findings.add("PROVENANCE_STRUCTURE_MISMATCH")
        verified: set[str] = set()
        replay_complete = False
        if stream_valid and source_valid and lineage_valid:
            verified, replay_complete = await self._replay(
                physical,
                records,
                values,
                checked_plan,
                checked_context,
                findings,
                boundary,
            )
        known_fields = {item.field for item in values}
        if normalized[-1].manifest is not None:
            known_fields.update(normalized[-1].manifest.semantic_index.fields)
        if any(
            binding.field not in known_fields for binding in self._policy.normalizations
        ):
            findings.add(
                "NORMALIZATION_EVIDENCE_UNEXPECTED", layer=ValidationLayer.NORMALIZATION
            )
        result_by_id: dict[str, NormalizationResult] = {}
        for trace in traces:
            if (
                trace.source_value is None
                or trace.source_value.value_id in result_by_id
            ):
                findings.add(
                    "NORMALIZATION_EVIDENCE_UNEXPECTED",
                    layer=ValidationLayer.NORMALIZATION,
                )
            else:
                result_by_id[trace.source_value.value_id] = trace
        evidence: list[ValueProvenanceEvidence] = []
        for item in values:
            await asyncio.sleep(0)
            candidate = result_by_id.pop(item.value.value_id, None)
            normalized_valid = self._normalization(
                item, candidate, item.value.value_id in verified, findings
            )
            evidence.append(
                ValueProvenanceEvidence(
                    record_index=item.record,
                    entity_index=item.entity,
                    value_index=item.index,
                    value_id=item.value.value_id,
                    raw_reference=ArtifactValueReference(
                        artifact_fingerprint=item.batch_fingerprint,
                        pointer=item.pointer + "/raw_value",
                    ),
                    normalized_reference=ArtifactValueReference(
                        artifact_fingerprint=candidate.fingerprint
                        if candidate
                        else item.batch_fingerprint,
                        pointer="/normalized_value"
                        if candidate
                        else item.pointer + "/normalized_value",
                    ),
                    locations=tuple(
                        SourceEvidenceLocation(
                            source_ref=o.source_ref,
                            location=o.location,
                            source_spans=o.source_spans,
                        )
                        for o in item.value.origins
                    ),
                    required=self._policy.require_null
                    if item.value.raw_value.kind == "null"
                    else self._policy.require_non_null,
                    verified=item.value.value_id in verified and normalized_valid,
                )
            )
        if result_by_id:
            findings.add(
                "NORMALIZATION_EVIDENCE_UNEXPECTED", layer=ValidationLayer.NORMALIZATION
            )
        manifest = normalized[-1].manifest
        lineage = ValidationLineage(
            source=expected_source,
            extraction_fingerprint=checked_context.extraction_fingerprint,
            parse_plan_fingerprint=checked_context.parse_plan_fingerprint,
            normalized_fingerprint=manifest.normalized_fingerprint
            if manifest
            else canonical_sha256_value(
                tuple(batch.canonical_json() for batch in normalized)
            ),
            policy_fingerprint=canonical_sha256_value(
                (
                    self._policy.canonical_json(),
                    self._parse_options.canonical_json(),
                    checked_plan.validation_fingerprint,
                )
            ),
            registry_fingerprint=self._registry.fingerprint if self._registry else None,
            normalization_fingerprints=tuple(
                sorted(trace.fingerprint for trace in traces)
            ),
        )
        required = (ValidationLayer.NORMALIZATION, ValidationLayer.PROVENANCE)
        completed = required if replay_complete else ()
        return build_report(
            run_id=checked_context.run_id,
            generated_at=generated_at,
            lineage=lineage,
            total_records=len(records),
            findings=tuple(findings.items),
            evidence=tuple(evidence),
            completed_layers=completed,
            required_layers=required,
        )

    async def _replay(
        self,
        physical: tuple[ExtractedBatch, ...],
        records: tuple[NormalizedRecord, ...],
        values: tuple[_Value, ...],
        plan: ValidatedParsePlan,
        context: ParseExecutionContext,
        findings: _Findings,
        boundary: BoundedInput,
    ) -> tuple[set[str], bool]:
        # M5 facade также экспортирует LLM adapters; они не нужны при импорте validator.
        from structuraguard.structure import ParsePlanExecutor

        refs = {ref for batch in physical for ref in batch.physical_refs()}
        indexed = {(item.record, item.entity, item.index): item for item in values}
        verified: set[str] = set()
        count = 0
        executor = ParsePlanExecutor(options=self._parse_options)
        try:
            async with aclosing(
                executor.execute(_stream(physical), plan, context)
            ) as replay:
                async for batch in replay:
                    for expected in batch.records:
                        count += 1
                        if count > self._policy.limits.max_records:
                            raise failure("SECURITY_LIMIT_EXCEEDED")
                        boundary.scan(expected)
                        if count <= len(records):
                            verified.update(
                                _compare_record(
                                    count - 1,
                                    records[count - 1],
                                    expected,
                                    indexed,
                                    refs,
                                    self._policy,
                                    findings,
                                )
                            )
                        await asyncio.sleep(0)
        except ParseExecutionError:
            findings.add("PROVENANCE_REPLAY_FAILED")
            return set(), False
        if count != len(records):
            findings.add("PROVENANCE_STRUCTURE_MISMATCH")
            return set(), True
        return verified, True

    def _normalization(
        self,
        item: _Value,
        trace: NormalizationResult | None,
        verified: bool,
        findings: _Findings,
    ) -> bool:
        layer = ValidationLayer.NORMALIZATION
        binding = next(
            (
                binding
                for binding in self._policy.normalizations
                if binding.field == item.field
            ),
            None,
        )
        if binding is None:
            if trace is not None:
                findings.add("NORMALIZATION_EVIDENCE_UNEXPECTED", item, layer=layer)
                return False
            return True
        if trace is None:
            findings.add(
                "NORMALIZATION_EVIDENCE_MISSING", item, layer=layer, unverified=True
            )
            return False
        if not verified:
            findings.add(
                "NORMALIZATION_PROVENANCE_UNVERIFIED",
                item,
                layer=layer,
                unverified=True,
            )
            return False
        assert self._registry is not None
        if (
            trace.source_value != item.value
            or trace.policy != binding.policy
            or trace.requested_steps != binding.steps
            or trace.registry_fingerprint != self._registry.fingerprint
        ):
            findings.add("NORMALIZATION_EVIDENCE_MISMATCH", item, layer=layer)
            return False
        try:
            replayed = self._registry.normalize_value(
                item.value, steps=binding.steps, policy=binding.policy
            )
        except ValidationError:
            findings.add("NORMALIZATION_EVIDENCE_MISMATCH", item, layer=layer)
            return False
        if trace.model_dump_json() != replayed.model_dump_json():
            findings.add("NORMALIZATION_EVIDENCE_MISMATCH", item, layer=layer)
            return False
        if not replayed.accepted:
            findings.add("NORMALIZATION_FAILED", item, layer=layer)
        return True
