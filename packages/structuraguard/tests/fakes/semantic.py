"""Synthetic fixtures semantic parsing с реальными M4/M5 и fixed IDs/clocks."""

from collections.abc import AsyncIterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal
from typing import cast

from structuraguard.contracts import (
    DataClassification,
    PhysicalObjectKind,
    PhysicalSourceRef,
    PipelineStatus,
    ProducerMetadata,
    SemanticParsingMode,
    StringScalar,
)
from structuraguard.contracts._base import CanonicalValue, canonical_json_value
from structuraguard.contracts.analysis import ExplicitRecordGrouping
from structuraguard.contracts.common import ParsePlanKind
from structuraguard.contracts.parsing import (
    DocumentParsePlan,
    DocumentTargetSelector,
    LogParsePlan,
    LogTokenSelector,
    ParseEntity,
    ParseField,
    ParsePlan,
    ParsePlanValidationRequest,
    ParsePlanValidationResult,
    PhysicalSample,
    StructureAnalysisRequest,
    StructureCandidate,
    StructureProfile,
    TabularColumnSelector,
    TabularParsePlan,
    TreeNodeGrouping,
    TreeParsePlan,
    TreePathSelector,
)
from structuraguard.contracts.reports import SecurityReport, SecurityScanRequest
from structuraguard.contracts.semantic import LLMAnalysisContext, LLMStructurePolicy
from structuraguard.contracts.source import ExtractedBatch
from structuraguard.parsers.builtin import MarkdownParser
from structuraguard.ports.parser import Parser
from structuraguard.structure import ParsePlanValidator, StructuralProfiler
from structuraguard.structure.semantic_samples import (
    SemanticSampleCatalog,
    prepare_samples,
)
from tests.fakes.llm import NOW, digest
from tests.unit.parsers.builtin._support import collect, contexts_for, source_for
from tests.unit.structure.test_execution import prepared, stream


@dataclass
class Scanner:
    """Разрешает только synthetic test data, не является production PII scanner."""

    requests: list[SecurityScanRequest] = dataclass_field(default_factory=list)

    async def scan(self, request: SecurityScanRequest) -> SecurityReport:
        self.requests.append(request)
        return SecurityReport.model_validate(
            {
                **request.model_dump(exclude={"payload_json"}),
                "producer": ProducerMetadata(
                    component_id="test_scanner",
                    component_version="1.0.0",
                    sdk_version="0.3.0",
                ),
                "decision": "allowed",
                "status": PipelineStatus.COMPLETED,
                "artifact_fingerprints": (
                    request.content_fingerprint,
                    request.payload_fingerprint,
                ),
                "scanned_items": 1,
                "generated_at": NOW,
            }
        )


class Validator(ParsePlanValidator):
    """Spy сохраняет только число обращений к настоящей physical validation."""

    calls = 0

    async def validate_source(
        self,
        request: ParsePlanValidationRequest | dict[str, object],
        batches: AsyncIterable[ExtractedBatch],
    ) -> ParsePlanValidationResult:
        self.calls += 1
        return await super().validate_source(request, batches)


def context() -> LLMAnalysisContext:
    return LLMAnalysisContext(
        run_id="semantic_test",
        data_classification=DataClassification.INTERNAL,
        routing_policy_id="test-local",
        routing_policy_fingerprint=digest("test-routing"),
        redaction_fingerprint=digest("synthetic-no-pii"),
    )


def samples_for(
    batches: tuple[ExtractedBatch, ...], maximum: int = 100
) -> tuple[PhysicalSample, ...]:
    samples: list[PhysicalSample] = []
    for batch in batches:

        def ref(
            kind: PhysicalObjectKind, local_id: str, batch: ExtractedBatch = batch
        ) -> PhysicalSourceRef:
            return PhysicalSourceRef(
                extraction_id=batch.extraction_id,
                batch_index=batch.batch_index,
                kind=kind,
                local_id=local_id,
            )

        for table in batch.tables:
            for cell in table.cells:
                if len(samples) < maximum:
                    samples.append(
                        PhysicalSample(
                            source_ref=ref(PhysicalObjectKind.CELL, cell.cell_id),
                            raw_value=cell.value.raw_value,
                            location=cell.value.location,
                            batch_fingerprint=batch.batch_fingerprint,
                        )
                    )
        for node in batch.trees:
            if node.value and len(samples) < maximum:
                samples.append(
                    PhysicalSample(
                        source_ref=ref(PhysicalObjectKind.TREE_NODE, node.node_id),
                        raw_value=node.value.raw_value,
                        location=node.location,
                        batch_fingerprint=batch.batch_fingerprint,
                    )
                )
        for line in batch.lines:
            if len(samples) < maximum:
                samples.append(
                    PhysicalSample(
                        source_ref=ref(PhysicalObjectKind.LINE, line.line_id),
                        raw_value=StringScalar(value=line.text),
                        location=line.location,
                        batch_fingerprint=batch.batch_fingerprint,
                    )
                )
        for block in batch.blocks:
            if block.text is not None and len(samples) < maximum:
                samples.append(
                    PhysicalSample(
                        source_ref=ref(PhysicalObjectKind.BLOCK, block.block_id),
                        raw_value=StringScalar(value=block.text),
                        location=block.location,
                        batch_fingerprint=batch.batch_fingerprint,
                    )
                )
    return tuple(samples)


def proposal(plan: ParsePlan, catalog: SemanticSampleCatalog) -> dict[str, object]:
    aliases = {entry.ref: alias for alias, entry in catalog.entries.items()}
    field_ids = {field.field_id: f"f{i}" for i, field in enumerate(plan.fields)}
    entity_ids = {entity.entity_id: f"e{i}" for i, entity in enumerate(plan.entities)}
    fields: list[dict[str, object]] = []
    for field in plan.fields:
        selector: dict[str, object] = dict(
            kind="log_record",
            index=None,
            offset=None,
            path=[],
            value_source=None,
            delimiter=None,
            target=None,
            key_equals=None,
        )
        original = field.selector
        if isinstance(original, TabularColumnSelector):
            selector.update(kind="column", index=original.column_index)
        elif isinstance(original, TreePathSelector):
            selector.update(
                kind="tree",
                path=[step.model_dump(mode="json") for step in original.steps],
                value_source=original.value_source,
            )
        elif isinstance(original, LogTokenSelector):
            selector.update(
                kind="log_piece",
                index=original.token_index,
                offset=original.line_offset,
                delimiter=original.delimiter,
            )
        elif isinstance(original, DocumentTargetSelector):
            selector.update(
                kind="document",
                offset=original.block_offset,
                target=original.target,
                key_equals=original.key_equals,
            )
        fields.append(
            dict(
                field_id=field_ids[field.field_id],
                semantic_name=field_ids[field.field_id],
                semantic_type="string",
                locale_hint="ru-RU",
                source_refs=[aliases[ref] for ref in field.source_refs],
                selector=selector,
            )
        )
    entities = []
    for entity in plan.entities:
        grouping = entity.grouping
        entities.append(
            dict(
                entity_id=entity_ids[entity.entity_id],
                entity_type="entry",
                parent_entity_id=entity_ids[entity.parent_entity_id]
                if entity.parent_entity_id
                else None,
                field_ids=[field_ids[field] for field in entity.field_ids],
                path=[step.model_dump(mode="json") for step in grouping.record_steps]
                if isinstance(grouping, TreeNodeGrouping)
                else [],
                records=[
                    [aliases[ref] for ref in record] for record in grouping.records
                ]
                if isinstance(grouping, ExplicitRecordGrouping)
                else [],
            )
        )
    body: dict[str, object] = dict(
        kind=plan.kind,
        root_ref=None,
        header_row=None,
        data_start_row=None,
        data_end_row=None,
        footer_start_row=None,
        repeated_header_rows=[],
        scope=[],
        fields=fields,
        entities=entities,
    )
    if isinstance(plan, TabularParsePlan):
        body.update(
            root_ref=aliases[plan.table_ref],
            header_row=plan.header_row,
            data_start_row=plan.data_start_row,
            data_end_row=plan.data_end_row,
            footer_start_row=plan.footer_start_row,
            repeated_header_rows=list(plan.repeated_header_rows),
        )
    elif isinstance(plan, TreeParsePlan):
        body.update(root_ref=aliases[plan.root_ref])
    elif isinstance(plan, LogParsePlan):
        body.update(scope=[aliases[ref] for ref in plan.line_refs])
    elif isinstance(plan, DocumentParsePlan):
        body.update(scope=[aliases[ref] for ref in plan.block_refs])
    return dict(
        schema_version="1.0.0",
        decision="plan",
        candidate_ids=list(catalog.candidates)[:1],
        self_confidence=0.01,
        plan=body,
    )


def encoded(value: dict[str, object]) -> str:
    return canonical_json_value(cast(CanonicalValue, value))


async def scenario(
    parser: Parser, content: bytes, *, batch_size: int = 100
) -> tuple[StructureAnalysisRequest, tuple[ExtractedBatch, ...], dict[str, object]]:
    if isinstance(parser, MarkdownParser):
        source = source_for(content, display_name="document")
        batches = await collect(
            parser, source, contexts_for(source, content, batch_size=batch_size)[1]
        )
        profile = await StructuralProfiler().profile(stream(batches))
        manifest = batches[-1].manifest
        assert manifest is not None
        refs = tuple(
            ref
            for ref in manifest.source_index.refs
            if ref.kind is PhysicalObjectKind.BLOCK
        )
        candidate = StructureCandidate(
            candidate_id="document_targets",
            source=manifest.source,
            extraction_fingerprint=manifest.extraction_fingerprint,
            plan_kind=ParsePlanKind.DOCUMENT,
            confidence=Decimal("0.9"),
            evidence=refs,
            observation_ids=tuple(item.evidence_id for item in profile.observations),
        )
        profile = StructureProfile.model_validate(
            {
                **profile.model_dump(),
                "candidates": (candidate,),
                "profile_fingerprint": "sha256:" + "0" * 64,
            }
        )
        plan = DocumentParsePlan(
            plan_id="document",
            schema_version="1.1.0",
            revision=1,
            source_fingerprint=manifest.source.source_fingerprint,
            extraction_fingerprint=manifest.extraction_fingerprint,
            profile_fingerprint=profile.profile_fingerprint,
            confidence=Decimal("0.9"),
            producer=profile.producer,
            block_refs=refs,
            fields=(
                ParseField(
                    field_id="name",
                    semantic_name="name",
                    semantic_type="unresolved",
                    source_refs=(refs[0], refs[-1]),
                    selector=DocumentTargetSelector(
                        target="value", block_offset=0, key_equals="Name"
                    ),
                ),
            ),
            entities=(
                ParseEntity(
                    entity_id="entry",
                    entity_type="entry",
                    field_ids=("name",),
                    grouping=ExplicitRecordGrouping(records=(refs[:-1], (refs[-1],))),
                ),
            ),
            evidence=refs,
        )
        checked = ParsePlanValidationRequest(
            plan=plan, source=manifest.source, manifest=manifest, profile=profile
        )
    else:
        checked, batches = await prepared(parser, content, batch_size=batch_size)
    request = StructureAnalysisRequest(
        source=checked.source,
        manifest=checked.manifest,
        profile=checked.profile,
        mode=SemanticParsingMode.LLM_ASSISTED,
        samples=samples_for(batches),
    )
    catalog = await prepare_samples(
        request, lambda: stream(batches), LLMStructurePolicy()
    )
    return request, batches, proposal(checked.plan, catalog)
