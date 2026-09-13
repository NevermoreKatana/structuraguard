"""M12 coordinator: точная projection, required layers и physical replay."""

import json

from structuraguard.contracts._base import canonical_sha256_value
from structuraguard.contracts.common import PipelineStatus as S
from structuraguard.contracts.constraint_validation import (
    ConstraintTablePolicy,
    ConstraintValidationPolicy,
)
from structuraguard.contracts.loading import DryRunRequest
from structuraguard.contracts.mapping import ValidatedMappingPlan
from structuraguard.contracts.normalization import NormalizationResult
from structuraguard.contracts.parsing import ParseExecutionContext
from structuraguard.contracts.provenance import (
    DetailedValidationReport,
    ValidationFinding,
    ValidationLayerResult,
)
from structuraguard.contracts.provenance import ValidationLayer as L
from structuraguard.contracts.record_validation import RecordValidationResult
from structuraguard.loading.projection import Prepared, prepare
from structuraguard.normalization import NormalizerRegistry
from structuraguard.validation import (
    BusinessRuleValidator,
    DatabaseConstraintValidator,
    JsonSchemaValidator,
    ProvenanceValidator,
    ValidationReportBuilder,
)

from .source import NormalizedData


def execution_context(data: NormalizedData) -> ParseExecutionContext:
    run = data.source._run
    return ParseExecutionContext(
        run_id=run.run_id,
        source_fingerprint=data.source.artifact.source_fingerprint,
        extraction_fingerprint=data.source.manifest.extraction_fingerprint,
        parse_plan_fingerprint=data.plan.plan.fingerprint,
        manifest=data.source.manifest,
        profile=run.result.structure_profile,
        max_records_per_batch=run.dependencies.parsing.records_per_batch,
    )


async def validate(
    data: NormalizedData, checked: ValidatedMappingPlan
) -> tuple[Prepared, DetailedValidationReport]:
    run = data.source._run
    deps = run.dependencies
    assert run.database is not None and run.result.database_report is not None
    binding, catalog = run.database, run.result.database_report
    registry = NormalizerRegistry.with_builtins().snapshot()
    results: list[NormalizationResult] = []

    async def normalize() -> Prepared:
        fields = {item.field: item for item in deps.provenance.normalizations}
        for batch in data.batches:
            for record in batch.records:
                for entity in record.entities:
                    for value in entity.values:
                        field = next(
                            (
                                field
                                for field in fields
                                if field.entity_type == entity.entity_type
                                and field.field_name == value.field_name
                            ),
                            None,
                        )
                        if field:
                            rule = fields[field]
                            result = registry.normalize_value(
                                value, steps=rule.steps, policy=rule.policy
                            )
                            results.append(result)
                            if (
                                not result.accepted
                                or result.normalized_value != value.normalized_value
                            ):
                                await run.stop("DRY_RUN_PROVENANCE_UNVERIFIED")
        return await prepare(
            DryRunRequest(batches=data.batches, mapping=checked.plan),
            max_bytes=deps.max_snapshot_bytes,
            max_records=deps.max_records,
        )

    prepared = await run.perform(S.NORMALIZING, normalize)

    async def records() -> DetailedValidationReport:
        provenance = await ProvenanceValidator(
            policy=deps.provenance,
            registry=registry,
            parse_options=deps.parsing.structural.execution,
        ).validate(
            data.batches,
            source_batches=data.source.batches,
            plan=data.plan,
            context=execution_context(data),
            generated_at=deps.clock(),
            normalizations=tuple(results),
        )
        run.set(validation_report=provenance)
        layers: list[ValidationLayerResult] = []
        required = [L.PROVENANCE, L.NORMALIZATION, L.DATABASE]
        input_fp = provenance.lineage.input_fingerprint
        source_indices = {
            record.record_id: index
            for index, record in enumerate(
                record for batch in data.batches for record in batch.records
            )
        }

        def add_record_result(layer: L, result: RecordValidationResult) -> None:
            findings = []
            for item in result.issues:
                origin = (
                    prepared.origins.get(item.record_id) if item.record_id else None
                )
                findings.append(
                    ValidationFinding(
                        layer=layer,
                        code=item.issue.code,
                        severity=item.issue.severity,
                        record_index=source_indices[origin.record_id]
                        if origin
                        else None,
                    )
                )
            layers.append(
                ValidationLayerResult(
                    layer=layer,
                    input_fingerprint=input_fp,
                    evidence_fingerprints=(canonical_sha256_value(result),),
                    complete=True,
                    findings=tuple(findings),
                )
            )

        identities = (
            {item.table_id: item.column_ids for item in checked.evidence.identities}
            if checked.evidence
            else {}
        )
        cp = binding.policy
        policy = ConstraintValidationPolicy(
            target_id=catalog.target_id,
            target_policy_fingerprint=catalog.target_policy_fingerprint,
            database_fingerprint=catalog.database_fingerprint,
            tables=tuple(
                ConstraintTablePolicy(
                    table_id=tid,
                    operation="upsert"
                    if checked.plan.operation.value == "upsert"
                    else "insert",
                    identity_column_ids=identities.get(tid, ())
                    if checked.plan.operation.value == "upsert"
                    else (),
                )
                for tid in sorted({m.target.table_id for m in checked.plan.mappings})
            ),
            allow_columns=cp.read_policy.allow_columns,
            source_identity_allow=cp.mapping_policy.source_identity_allow,
            checks=cp.checks,
        )
        db_result = await DatabaseConstraintValidator(
            policy, reader=binding.reader
        ).validate(prepared.data, catalog=catalog)
        add_record_result(L.DATABASE, db_result)
        if deps.business_rules:
            required.append(L.BUSINESS_RULES)
            business = await BusinessRuleValidator().validate(
                prepared.data, rules=deps.business_rules
            )
            add_record_result(L.BUSINESS_RULES, business)
        if deps.json_schema is not None:
            required.append(L.JSON_SCHEMA)
            instance = [
                {
                    "table": record.collection_id,
                    "values": {
                        cell.field_id: cell.value.model_dump(mode="python").get("value")
                        for cell in record.values
                    },
                }
                for record in prepared.data.records
            ]
            schema = await JsonSchemaValidator().validate(
                instance, schema=json.loads(deps.json_schema)
            )
            findings = tuple(
                ValidationFinding(
                    layer=L.JSON_SCHEMA,
                    code="JSON_SCHEMA_VALIDATION_FAILED",
                    check_index=index,
                )
                for index, _ in enumerate(schema.issues)
            )
            if not schema.schema_valid and not findings:
                findings = (
                    ValidationFinding(layer=L.JSON_SCHEMA, code="JSON_SCHEMA_INVALID"),
                )
            layers.append(
                ValidationLayerResult(
                    layer=L.JSON_SCHEMA,
                    input_fingerprint=input_fp,
                    evidence_fingerprints=(canonical_sha256_value(schema),),
                    complete=True,
                    findings=findings,
                )
            )
        report = ValidationReportBuilder(
            required_layers=tuple(required), limits=deps.provenance.limits
        ).combine(provenance, layers=tuple(layers))
        run.set(validation_report=report)
        return report

    report = await run.perform(S.VALIDATING, records)
    return prepared, report
