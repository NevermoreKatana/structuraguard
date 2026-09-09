from __future__ import annotations

import hashlib
import json
from collections.abc import (
    Mapping,
    MutableMapping,
    MutableSequence,
    MutableSet,
    Sequence,
)
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime
from decimal import Decimal
from typing import get_args, get_origin

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

import structuraguard
import structuraguard.contracts as contracts

_M1_ROOT_EXPORTS = frozenset(
    {
        "AsyncStructuraGuard",
        "DatabaseInspectionError",
        "LoadError",
        "MappingError",
        "OperationNotImplementedError",
        "ParserError",
        "SDKConfig",
        "SecurityPolicyError",
        "SourceError",
        "StructuraGuard",
        "StructuraGuardError",
        "ValidationError",
    }
)

_REQUIRED_CONTRACT_EXPORTS = frozenset(
    {
        "AuditEvent",
        "ColumnCatalog",
        "DatabaseCatalog",
        "DocumentParsePlan",
        "ExtractedBatch",
        "ExtractedBlock",
        "ExtractedCell",
        "ExtractedLine",
        "ExtractedTable",
        "ExtractedTreeNode",
        "ExtractedValue",
        "ForeignKeyCatalog",
        "LoadReport",
        "LogParsePlan",
        "MappingCandidate",
        "MappingPlan",
        "NormalizedBatch",
        "NormalizedRecord",
        "NormalizedValue",
        "ParseField",
        "ParsePlan",
        "ParseRule",
        "PipelineStatus",
        "ProbeResult",
        "SchemaCatalog",
        "SecurityReport",
        "SecurityApproval",
        "SemanticEntity",
        "SemanticParseReport",
        "SourceArtifact",
        "SourceLocation",
        "StructureCandidate",
        "StructureProfile",
        "TableCatalog",
        "TabularParsePlan",
        "TreeParsePlan",
        "ValidationReport",
    }
)

_PUBLIC_MODEL_NAMES = tuple(
    name
    for name in contracts.__all__
    if isinstance(getattr(contracts, name), type)
    and issubclass(getattr(contracts, name), BaseModel)
)

_PIPELINE_STATUS_VALUES = (
    "CREATED",
    "SOURCE_PROBING",
    "TECHNICAL_PARSING",
    "STRUCTURE_PROFILING",
    "STRUCTURE_ANALYZING",
    "PARSE_PLAN_CREATED",
    "PARSE_PLAN_VALIDATING",
    "SEMANTIC_PARSING",
    "NORMALIZED_DATA_PROFILING",
    "DATABASE_INSPECTING",
    "MAPPING",
    "MAPPING_PLAN_CREATED",
    "MAPPING_PLAN_VALIDATING",
    "NORMALIZING",
    "VALIDATING",
    "STAGING",
    "LOADING",
    "COMPLETED",
    "COMPLETED_WITH_WARNINGS",
    "NEEDS_REVIEW",
    "REJECTED_SECURITY",
    "ROLLED_BACK",
    "FAILED",
    "CANCELLED",
)

_BUILT_IN_ERROR_CODE_VALUES = (
    "SDK_OPERATION_NOT_IMPLEMENTED",
    "SYNC_API_IN_ASYNC_CONTEXT",
    "SECURITY_SANDBOX_REQUIRED",
    "PARSER_NO_TEXT_LAYER",
    "SOURCE_FINGERPRINT_MISMATCH",
    "SOURCE_PATH_NOT_ALLOWED",
    "SOURCE_SNAPSHOT_EXPIRED",
    "DATABASE_FINGERPRINT_MISMATCH",
    "DATABASE_TARGET_MISMATCH",
    "AUDIT_DURABILITY_REQUIRED",
    "PROCESSING_TIMEOUT",
    "DDL_FORBIDDEN",
    "TARGET_NOT_ALLOWED",
    "CONTRACT_VERSION_UNSUPPORTED",
    "PROVENANCE_INVALID",
    "PARSE_PLAN_INVALID",
    "MAPPING_PLAN_INVALID",
    "PROMPT_INJECTION_DETECTED",
    "SECURITY_LIMIT_EXCEEDED",
    "SECURITY_INPUT_REJECTED",
    "LLM_DATA_ROUTING_FORBIDDEN",
    "LLM_OUTPUT_INVALID",
    "PARSER_INVALID_ADAPTER",
    "PARSER_DUPLICATE_REGISTRATION",
    "PARSER_REGISTRY_FROZEN",
    "PARSER_SESSION_CLOSED",
    "PARSER_NOT_FOUND",
    "PARSER_UNSUPPORTED_FORMAT",
    "PARSER_DEPENDENCY_UNAVAILABLE",
    "PARSER_MALFORMED_INPUT",
    "PARSER_UNSUPPORTED_FEATURE",
    "PARSER_ENCODING_UNSUPPORTED",
    "PARSER_PROBE_FAILED",
    "PARSER_PROBE_INVALID",
    "PARSER_FORMAT_CONFLICT",
    "PARSER_PLUGIN_METADATA_INVALID",
    "PARSER_PLUGIN_DISCOVERY_FAILED",
    "PARSER_OUTPUT_INVALID",
)

_BUILT_IN_ISSUE_CODE_VALUES = (
    "INVALID_SOURCE_LOCATION",
    "DUPLICATE_ARTIFACT_ID",
    "INVALID_BATCH_SEQUENCE",
    "PROVENANCE_REFERENCE_MISSING",
    "UPSTREAM_FINGERPRINT_MISMATCH",
    "UNKNOWN_PLAN_OPERATOR",
    "PLAN_CONTAINS_EXECUTABLE_CONTENT",
    "AMBIGUOUS_STRUCTURE",
    "UNSUPPORTED_CONTRACT_VERSION",
    "PARSER_DECLARED_MIME_MISMATCH",
    "PARSER_EXTENSION_MISMATCH",
)

_SOURCE_FINGERPRINT = "sha256:" + "a" * 64


def _source_payload() -> dict[str, object]:
    return {
        "artifact_id": "source-1",
        "schema_version": "1.0.0",
        "display_name": "orders.csv",
        "media_type": "text/csv",
        "size_bytes": 128,
        "source_fingerprint": _SOURCE_FINGERPRINT,
    }


def _source_ref_payload(*, fingerprint: str = _SOURCE_FINGERPRINT) -> dict[str, object]:
    return {
        "artifact_id": "source-1",
        "source_fingerprint": fingerprint,
    }


def _line_location_payload() -> dict[str, object]:
    return {
        "kind": "line_range",
        "source": _source_ref_payload(),
        "line_start": 1,
        "line_end": 2,
    }


def _normalized_value_payload(
    scalar: dict[str, object],
    *,
    semantic_type: str = "money",
) -> dict[str, object]:
    return {
        "value_id": "value-total-amount",
        "field_name": "total_amount",
        "raw_value": {"kind": "string", "value": "125500.50"},
        "normalized_value": scalar,
        "semantic_type": semantic_type,
        "source_refs": [
            {
                "extraction_id": "extraction-1",
                "batch_index": 0,
                "kind": "block",
                "local_id": "block-1",
            }
        ],
        "transformations": ["normalize_scalar"],
    }


def _llm_request_payload(
    payload_json: str = '{"sample":"masked"}',
) -> dict[str, object]:
    payload_fingerprint = (
        "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    )
    security_report = contracts.SecurityReport(
        request_id="security-request-1",
        run_id="run-1",
        purpose="llm_input",
        content_fingerprint=_SOURCE_FINGERPRINT,
        payload_fingerprint=payload_fingerprint,
        data_classification=contracts.DataClassification.INTERNAL,
        routing_policy_id="policy-1",
        routing_policy_fingerprint="4" * 64,
        redaction_fingerprint="1" * 64,
        producer=contracts.ProducerMetadata(
            component_id="security-scanner",
            component_version="1.0.0",
            sdk_version="0.2.0",
        ),
        decision="allowed",
        status=contracts.PipelineStatus.COMPLETED,
        artifact_fingerprints=(_SOURCE_FINGERPRINT, payload_fingerprint),
        scanned_items=1,
        generated_at=datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
    )
    security_approval = contracts.SecurityApproval(
        report=security_report,
        report_fingerprint=(
            "sha256:"
            + hashlib.sha256(
                security_report.canonical_json().encode("utf-8")
            ).hexdigest()
        ),
    )
    return {
        "request_id": "request-1",
        "run_id": "run-1",
        "purpose": "semantic_parsing",
        "response_schema_id": "parse-plan",
        "response_schema_version": "1.0.0",
        "payload_json": payload_json,
        "payload_fingerprint": payload_fingerprint,
        "content_fingerprint": _SOURCE_FINGERPRINT,
        "data_classification": "INTERNAL",
        "routing_policy_id": "policy-1",
        "routing_policy_fingerprint": "4" * 64,
        "redaction_fingerprint": "1" * 64,
        "security_approval": security_approval,
        "prompt_fingerprint": "2" * 64,
        "max_output_bytes": 4096,
    }


def _contains_any(annotation: object) -> bool:
    if annotation is object:
        return False
    if str(annotation) == "typing.Any":
        return True
    return any(_contains_any(item) for item in get_args(annotation))


def _contains_mutable_collection(annotation: object) -> bool:
    origin = get_origin(annotation) or annotation
    if origin in (Mapping, Sequence, AbstractSet):
        return True
    if isinstance(origin, type) and issubclass(
        origin,
        (MutableMapping, MutableSequence, MutableSet),
    ):
        return True
    return any(_contains_mutable_collection(item) for item in get_args(annotation))


def test_m2_is_exported_only_from_dedicated_subpackages() -> None:
    root_exports = tuple(structuraguard.__all__)
    contract_exports = tuple(contracts.__all__)

    assert frozenset(root_exports) == _M1_ROOT_EXPORTS
    assert len(root_exports) == len(set(root_exports))
    assert frozenset(contract_exports) >= _REQUIRED_CONTRACT_EXPORTS
    assert len(contract_exports) == len(set(contract_exports))
    assert _REQUIRED_CONTRACT_EXPORTS.isdisjoint(root_exports)


@pytest.mark.parametrize("model_name", _PUBLIC_MODEL_NAMES)
def test_public_dto_contract_is_frozen_strict_and_typed(model_name: str) -> None:
    model = getattr(contracts, model_name)

    assert isinstance(model, type)
    assert issubclass(model, BaseModel)
    assert model.model_config.get("frozen") is True
    assert model.model_config.get("extra") == "forbid"

    for field_name, field in model.model_fields.items():
        assert not isinstance(field.default, (dict, list, set))
        assert not _contains_any(field.annotation), (
            f"{model_name}.{field.alias or field_name} exposes Any"
        )


@pytest.mark.parametrize("model_name", _PUBLIC_MODEL_NAMES)
def test_public_dto_annotations_do_not_expose_mutable_collections(
    model_name: str,
) -> None:
    model = getattr(contracts, model_name)

    for field_name, field in model.model_fields.items():
        assert not _contains_mutable_collection(field.annotation), (
            f"{model_name}.{field.alias or field_name} exposes a mutable collection"
        )


@pytest.mark.parametrize(
    "annotation",
    (
        MutableMapping[str, int],
        MutableSequence[int],
        MutableSet[str],
        Mapping[str, int],
        Sequence[int],
        AbstractSet[str],
        dict[str, int],
        list[int],
        set[str],
    ),
)
def test_mutable_collection_detector_covers_collections_abc_aliases(
    annotation: object,
) -> None:
    assert _contains_mutable_collection(annotation)
    assert not _contains_mutable_collection(tuple[str, ...])
    assert not _contains_mutable_collection(frozenset[str])
    assert not _contains_mutable_collection(str)


def test_source_artifact_has_value_object_semantics() -> None:
    first = contracts.SourceArtifact.model_validate(_source_payload())
    second = contracts.SourceArtifact.model_validate(_source_payload())
    changed_payload = {**_source_payload(), "size_bytes": 129}
    changed = contracts.SourceArtifact.model_validate(changed_payload)

    assert first == second
    assert first != changed
    assert first.model_dump_json() == second.model_dump_json()
    assert (
        contracts.SourceArtifact.model_validate_json(first.model_dump_json()) == first
    )

    with pytest.raises(ValidationError):
        first.display_name = "changed.csv"

    with pytest.raises(ValidationError):
        contracts.SourceArtifact.model_validate({**_source_payload(), "dsn": "secret"})


def test_source_location_is_a_closed_discriminated_union() -> None:
    adapter: TypeAdapter[contracts.SourceLocation] = TypeAdapter(
        contracts.SourceLocation
    )
    schema = adapter.json_schema()
    discriminator = schema["discriminator"]

    assert discriminator["propertyName"] == "kind"
    assert (
        set(discriminator["mapping"])
        == {
            "line_range",
            "tabular_cell",
            "sheet_cell",
            "json_pointer",
            "xpath",
            "css_selector",
            "document_block",
            "extension",
        }
        <= set(discriminator["mapping"])
    )

    location = adapter.validate_python(_line_location_payload())
    restored = adapter.validate_json(adapter.dump_json(location))

    assert restored == location
    assert location.source.artifact_id == "source-1"
    assert location.source.source_fingerprint == _SOURCE_FINGERPRINT
    assert adapter.dump_json(location) == adapter.dump_json(location)


@pytest.mark.parametrize(
    "payload",
    (
        {
            "kind": "line_range",
            "line_start": 1,
            "line_end": 2,
        },
        {
            **_line_location_payload(),
            "line_start": 3,
            "line_end": 2,
        },
        {
            **_line_location_payload(),
            "row_index": 0,
        },
        {
            "kind": "unknown",
            "source": _source_ref_payload(),
        },
    ),
)
def test_source_location_rejects_unverifiable_or_hybrid_states(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(contracts.SourceLocation).validate_python(payload)


def test_extracted_value_preserves_raw_physical_value_without_business_meaning() -> (
    None
):
    value = contracts.ExtractedValue.model_validate(
        {
            "raw_value": {"kind": "decimal", "value": "125500.50"},
            "location": _line_location_payload(),
            "technical_type_hint": "decimal",
        }
    )
    restored = contracts.ExtractedValue.model_validate_json(value.model_dump_json())

    assert restored == value
    assert restored.raw_value.value == Decimal("125500.50")
    assert "semantic_name" not in contracts.ExtractedValue.model_fields
    assert "semantic_type" not in contracts.ExtractedValue.model_fields
    assert "entity_type" not in contracts.ExtractedBlock.model_fields
    assert "target_table" not in contracts.ExtractedBatch.model_fields
    assert "parse_plan_fingerprint" not in contracts.ExtractedBatch.model_fields
    assert "parse_plan_fingerprint" in contracts.NormalizedBatch.model_fields

    with pytest.raises(ValidationError):
        contracts.ExtractedValue.model_validate(
            {
                "raw_value": {"kind": "string", "value": "customer"},
                "location": _line_location_payload(),
                "semantic_type": "customer_name",
            }
        )


def test_normalized_value_is_deeply_immutable_and_round_trips_decimal() -> None:
    source_refs = [
        {
            "extraction_id": "extraction-1",
            "batch_index": 0,
            "kind": "block",
            "local_id": "block-1",
        }
    ]
    transformations = ["parse_decimal"]
    payload = _normalized_value_payload(
        {"kind": "decimal", "value": Decimal("125500.50")}
    )
    payload["source_refs"] = source_refs
    payload["transformations"] = transformations

    value = contracts.NormalizedValue.model_validate(payload)
    serialized = value.model_dump_json()
    restored = contracts.NormalizedValue.model_validate_json(serialized)
    source_refs.append(
        {
            "extraction_id": "extraction-1",
            "batch_index": 0,
            "kind": "block",
            "local_id": "block-2",
        }
    )
    transformations.append("mutated_after_validation")

    assert restored == value
    assert value.normalized_value.value == Decimal("125500.50")
    assert '"125500.50"' in serialized
    assert value.model_dump_json() == serialized
    assert isinstance(value.source_refs, tuple)
    assert isinstance(value.transformations, tuple)
    assert len(value.source_refs) == 1
    assert value.transformations == ("parse_decimal",)

    with pytest.raises(ValidationError):
        value.field_name = "changed"


def test_normalized_scalar_keeps_bool_integer_and_decimal_distinct() -> None:
    integer_scalar = contracts.IntegerScalar(value=1)
    boolean_scalar = contracts.BooleanScalar(value=True)
    decimal_scalar = contracts.DecimalScalar(value=Decimal("1"))
    integer = contracts.NormalizedValue.model_validate(
        _normalized_value_payload(
            {"kind": "integer", "value": 1}, semantic_type="integer"
        )
    )
    boolean = contracts.NormalizedValue.model_validate(
        _normalized_value_payload(
            {"kind": "boolean", "value": True}, semantic_type="boolean"
        )
    )
    decimal = contracts.NormalizedValue.model_validate(
        _normalized_value_payload({"kind": "decimal", "value": "1.0"})
    )

    assert integer_scalar == contracts.IntegerScalar(value=1)
    assert integer_scalar != contracts.IntegerScalar(value=2)
    assert integer_scalar != boolean_scalar
    assert boolean_scalar != decimal_scalar
    assert integer_scalar != decimal_scalar
    assert integer.normalized_value == integer_scalar
    assert boolean.normalized_value == boolean_scalar
    assert decimal.normalized_value == decimal_scalar
    assert integer != boolean
    assert boolean != decimal
    assert integer != decimal
    assert isinstance(integer.normalized_value.value, int)
    assert not isinstance(integer.normalized_value.value, bool)
    assert isinstance(boolean.normalized_value.value, bool)
    assert isinstance(decimal.normalized_value.value, Decimal)


def test_normalized_datetime_must_be_timezone_aware_utc() -> None:
    utc_value = contracts.NormalizedValue.model_validate(
        _normalized_value_payload(
            {
                "kind": "datetime",
                "value": datetime(2026, 9, 2, 12, 30, tzinfo=UTC),
            },
            semantic_type="datetime",
        )
    )

    assert isinstance(utc_value.normalized_value, contracts.DateTimeScalar)
    assert utc_value.normalized_value.value.tzinfo is UTC
    assert (
        contracts.NormalizedValue.model_validate_json(utc_value.model_dump_json())
        == utc_value
    )

    with pytest.raises(ValidationError):
        contracts.NormalizedValue.model_validate(
            _normalized_value_payload(
                {
                    "kind": "datetime",
                    "value": datetime(2026, 9, 2, 12, 30),
                },
                semantic_type="datetime",
            )
        )


@pytest.mark.parametrize("value", ("NaN", "Infinity", "-Infinity"))
def test_normalized_decimal_rejects_non_finite_values(value: str) -> None:
    with pytest.raises(ValidationError):
        contracts.NormalizedValue.model_validate(
            _normalized_value_payload({"kind": "decimal", "value": value})
        )


def test_normalized_value_requires_physical_provenance() -> None:
    payload = _normalized_value_payload({"kind": "string", "value": "order-1"})
    payload["source_refs"] = []

    with pytest.raises(ValidationError):
        contracts.NormalizedValue.model_validate(payload)


def test_parse_plan_and_rules_are_closed_discriminated_unions() -> None:
    plan_schema = TypeAdapter(contracts.ParsePlan).json_schema()
    rule_schema = TypeAdapter(contracts.ParseRule).json_schema()

    assert plan_schema["discriminator"]["propertyName"] == "kind"
    assert set(plan_schema["discriminator"]["mapping"]) == {
        "tabular",
        "tree",
        "log",
        "document",
    }
    assert rule_schema["discriminator"]["propertyName"] == "kind"
    assert {"record_range", "group_lines"} <= set(
        rule_schema["discriminator"]["mapping"]
    )

    rule_adapter: TypeAdapter[contracts.ParseRule] = TypeAdapter(contracts.ParseRule)
    rule = rule_adapter.validate_python(
        {"kind": "record_range", "start_index": 0, "end_index": 10}
    )
    assert rule_adapter.validate_json(rule_adapter.dump_json(rule)) == rule

    with pytest.raises(ValidationError):
        TypeAdapter(contracts.ParseRule).validate_python(
            {"kind": "python", "code": "__import__('os')"}
        )


@pytest.mark.parametrize(
    "model",
    (
        contracts.TabularParsePlan,
        contracts.TreeParsePlan,
        contracts.LogParsePlan,
        contracts.DocumentParsePlan,
        contracts.ParseField,
        contracts.RecordRangeRule,
        contracts.GroupLinesRule,
    ),
)
def test_parse_plan_schema_excludes_mapping_and_database_namespace(
    model: type[BaseModel],
) -> None:
    forbidden_fields = {
        "catalog",
        "database_fingerprint",
        "dsn",
        "load_policy",
        "mappings",
        "normalized_fingerprint",
        "sql",
        "target_column",
        "target_id",
        "target_policy_fingerprint",
        "target_table",
    }

    assert forbidden_fields.isdisjoint(model.model_fields)


@pytest.mark.parametrize(
    "model",
    (
        contracts.MappingPlan,
        contracts.FieldMapping,
        contracts.SemanticFieldRef,
        contracts.CatalogColumnRef,
    ),
)
def test_mapping_plan_schema_excludes_physical_parse_namespace(
    model: type[BaseModel],
) -> None:
    forbidden_fields = {
        "batch_index",
        "block_refs",
        "bounding_box",
        "column_index",
        "data_end_row",
        "data_start_row",
        "header_row",
        "line_refs",
        "line_end",
        "line_start",
        "location",
        "max_lines_per_record",
        "page_number",
        "pointer",
        "record_path",
        "root_ref",
        "selector",
        "sheet_name",
        "source_refs",
        "table_ref",
        "xpath",
        "row_index",
    }

    assert forbidden_fields.isdisjoint(model.model_fields)


@pytest.mark.parametrize(
    ("model", "field", "payload"),
    (
        (contracts.TabularParsePlan, "code", "print('unsafe')"),
        (contracts.TreeParsePlan, "callback", "module:function"),
        (contracts.LogParsePlan, "shell", "sh -c true"),
        (contracts.DocumentParsePlan, "sql", "DROP TABLE users"),
        (contracts.MappingPlan, "sql", "INSERT INTO users VALUES (1)"),
        (contracts.MappingPlan, "source_path", "/physical/source/path"),
        (contracts.TabularParsePlan, "target_id", "main"),
        (contracts.TreeParsePlan, "database_fingerprint", "f" * 64),
        (
            contracts.MappingPlan,
            "source_refs",
            (
                {
                    "extraction_id": "extraction-1",
                    "batch_index": 0,
                    "kind": "table",
                    "local_id": "table-1",
                },
            ),
        ),
    ),
)
def test_plans_do_not_expose_executable_or_cross_layer_fields(
    model: type[BaseModel], field: str, payload: object
) -> None:
    assert field not in model.model_fields

    with pytest.raises(ValidationError) as captured:
        model.model_validate({field: payload})

    assert any(error["loc"] == ("<redacted>",) for error in captured.value.errors())
    assert field not in str(captured.value)


def test_field_mapping_does_not_expose_transform_execution_hooks() -> None:
    assert "transformations" not in contracts.FieldMapping.model_fields

    with pytest.raises(ValidationError):
        contracts.FieldMapping.model_validate(
            {
                "source": {"entity_type": "order", "field_name": "total"},
                "target": {"table_id": "orders", "column_id": "total"},
                "transformations": ("DROP TABLE orders",),
            }
        )


def test_database_contracts_do_not_expose_credentials() -> None:
    forbidden_names = {"dsn", "password", "credentials", "connection"}

    assert forbidden_names.isdisjoint(contracts.DatabaseCatalog.model_fields)
    assert forbidden_names.isdisjoint(contracts.MappingPlan.model_fields)


@pytest.mark.parametrize(
    "forbidden_field",
    ("tools", "credentials", "dsn", "source_handle", "database_handle", "sql"),
)
def test_llm_request_rejects_tools_credentials_and_handles(
    forbidden_field: str,
) -> None:
    assert forbidden_field not in contracts.LLMRequest.model_fields

    payload = {**_llm_request_payload(), forbidden_field: "forbidden"}

    with pytest.raises(ValidationError):
        contracts.LLMRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    (
        ("tools", "forbidden"),
        ("tools.", "forbidden"),
        ("toolCalls", "forbidden"),
        ("credentials", "forbidden"),
        ("credentials ", "forbidden"),
        ("secrets", "forbidden"),
        ("tokens", "forbidden"),
        ("apiKeys", "forbidden"),
        ("apiKey", "sk-proj-supersecret"),
        ("APIKey", "hunter2"),
        ("X-Api-Key", "hunter2"),
        ("openaiApiKey", "hunter2"),
        ("api_key", "sk-secret"),
        ("access_token", "hunter2"),
        ("refresh_token", "hunter2"),
        ("client_secret", "hunter2"),
        ("passwd", "hunter2"),
        ("pwd", "hunter2"),
        ("dsn", "forbidden"),
        ("source_handle", "forbidden"),
        ("database_handle", "forbidden"),
        ("sql", "forbidden"),
        ("sql.query", "forbidden"),
        ("pass\u200bword", "forbidden"),
        ("se\u200bcrets", "forbidden"),
        ("s.q.l", "forbidden"),
        ("d-s-n", "forbidden"),
        ("to.ol.s", "forbidden"),
        ("apiKeyValue", "forbidden"),
        ("apiKeysByProvider", "forbidden"),
        ("privateKeyData", "forbidden"),
        ("databaseHandleId", "forbidden"),
        ("connectionStringValue", "forbidden"),
    ),
)
def test_llm_request_rejects_forbidden_keys_inside_structured_payload(
    forbidden_key: str,
    forbidden_value: str,
) -> None:
    payload_json = json.dumps(
        {"sample": {forbidden_key: forbidden_value}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValidationError):
        contracts.LLMRequest.model_validate(_llm_request_payload(payload_json))


@pytest.mark.parametrize(
    "missing_field",
    (
        "dry_run",
        "error_policy",
        "target_policy_fingerprint",
        "allowlist_ref",
        "denylist_ref",
        "staging_required",
        "transactional_audit_required",
        "rollback_required",
    ),
)
def test_load_policy_requires_every_explicit_safety_field(
    missing_field: str,
) -> None:
    payload: dict[str, object] = {
        "dry_run": False,
        "error_policy": "atomic",
        "target_policy_fingerprint": "1" * 64,
        "allowlist_ref": "allowlist-1",
        "denylist_ref": None,
        "staging_required": True,
        "transactional_audit_required": True,
        "rollback_required": True,
    }
    payload.pop(missing_field)

    with pytest.raises(ValidationError):
        contracts.LoadPolicy.model_validate(payload)


@pytest.mark.parametrize(
    "disabled_guarantee",
    ("staging_required", "transactional_audit_required", "rollback_required"),
)
def test_non_dry_run_load_policy_rejects_disabled_guarantees(
    disabled_guarantee: str,
) -> None:
    payload: dict[str, object] = {
        "dry_run": False,
        "error_policy": "atomic",
        "target_policy_fingerprint": "1" * 64,
        "allowlist_ref": "allowlist-1",
        "denylist_ref": None,
        "staging_required": True,
        "transactional_audit_required": True,
        "rollback_required": True,
    }
    payload[disabled_guarantee] = False

    with pytest.raises(ValidationError):
        contracts.LoadPolicy.model_validate(payload)


@pytest.mark.parametrize(
    "missing_field",
    (
        "run_id",
        "target_id",
        "database_fingerprint",
        "policy",
        "idempotency_key",
        "cancellation_token_id",
        "staging_capability_evidence",
        "audit_capability_evidence",
    ),
)
def test_load_context_requires_target_policy_and_capability_evidence(
    missing_field: str,
) -> None:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "target_id": "main",
        "database_fingerprint": "f" * 64,
        "policy": {
            "dry_run": False,
            "error_policy": "atomic",
            "target_policy_fingerprint": "1" * 64,
            "allowlist_ref": "allowlist-1",
            "denylist_ref": None,
            "staging_required": True,
            "transactional_audit_required": True,
            "rollback_required": True,
        },
        "idempotency_key": "load-1",
        "deadline": datetime(2026, 9, 3, 12, 0, tzinfo=UTC),
        "cancellation_token_id": "cancel-1",
        "staging_capability_evidence": "staging-capability-1",
        "audit_capability_evidence": "audit-capability-1",
    }
    assert contracts.LoadContext.model_validate(payload).target_id == "main"
    payload.pop(missing_field)

    with pytest.raises(ValidationError):
        contracts.LoadContext.model_validate(payload)


def test_pipeline_status_wire_values_are_stable_and_complete() -> None:
    values = tuple(status.value for status in contracts.PipelineStatus)

    assert values == _PIPELINE_STATUS_VALUES


def test_closed_wire_vocabularies_are_stable_and_complete() -> None:
    expected = {
        contracts.SemanticParsingMode: (
            "deterministic",
            "llm_assisted",
            "llm_first",
        ),
        contracts.DataClassification: (
            "PUBLIC",
            "INTERNAL",
            "CONFIDENTIAL",
            "RESTRICTED",
        ),
        contracts.ParsePlanKind: ("tabular", "tree", "log", "document"),
        contracts.ExtractedBlockKind: (
            "line",
            "paragraph",
            "heading",
            "list",
            "key_value",
            "metadata",
            "extension",
        ),
        contracts.PhysicalObjectKind: (
            "line",
            "block",
            "table",
            "cell",
            "tree_node",
            "value",
            "extension",
        ),
        contracts.ValidationDecision: ("accepted", "rejected", "needs_review"),
        contracts.IssueSeverity: ("info", "warning", "error", "critical"),
        contracts.LoadOperation: ("insert_only", "upsert"),
        contracts.ErrorPolicy: (
            "atomic",
            "quarantine_invalid",
            "best_effort",
        ),
        contracts.TransactionOutcome: (
            "not_started",
            "dry_run",
            "committed",
            "rolled_back",
            "unknown",
        ),
        contracts.BuiltInErrorCode: _BUILT_IN_ERROR_CODE_VALUES,
        contracts.BuiltInIssueCode: _BUILT_IN_ISSUE_CODE_VALUES,
    }

    for enum_type, values in expected.items():
        assert tuple(item.value for item in enum_type) == values


@pytest.mark.parametrize(
    "enum_type",
    (
        contracts.SemanticParsingMode,
        contracts.DataClassification,
        contracts.ParsePlanKind,
        contracts.ExtractedBlockKind,
        contracts.PhysicalObjectKind,
        contracts.PipelineStatus,
        contracts.ValidationDecision,
        contracts.IssueSeverity,
        contracts.LoadOperation,
        contracts.ErrorPolicy,
        contracts.TransactionOutcome,
        contracts.BuiltInErrorCode,
        contracts.BuiltInIssueCode,
    ),
)
def test_closed_wire_vocabularies_reject_unknown_values(
    enum_type: type[object],
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(enum_type).validate_python("unknown-vocabulary-value")
