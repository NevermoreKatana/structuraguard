"""Базовые value objects и стабильные wire-словари M2."""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictBool,
    StrictBytes,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationError,
    ValidatorFunctionWrapHandler,
    WrapValidator,
    model_validator,
)

from structuraguard.contracts._base import FrozenContract, _redacted_validation_error

_CREDENTIAL_CANARY = re.compile(
    r"(?i)(?:"
    r"\b(?:authorization|dsn|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"aws[_-]?secret[_-]?access[_-]?key|secret|token)\s*[:=]\s*\S+"
    r"|\b(?:bearer|basic)\s+(?=\S{8,}(?:\s|$))(?=\S*[0-9._~+/=-])\S+"
    r"|\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:sk-[A-Za-z0-9_-]{6,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|"
    r"whsec_[A-Za-z0-9]{12,})\b"
    r")"
)
_DATE_TEXT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DATETIME_TEXT = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
    r"(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_LOWER_HEX_DIGITS = frozenset("0123456789abcdef")
_GENERATED_ID_SUFFIX = (
    r"(?:0|[1-9][0-9]{0,9}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    r"|[0-7][0-9A-HJKMNP-TV-Z]{25})"
)
_EXTRACTION_ID = re.compile(rf"^extraction-{_GENERATED_ID_SUFFIX}$")


def _contains_credential_canary(value: str) -> bool:
    return _CREDENTIAL_CANARY.search(unicodedata.normalize("NFKC", value)) is not None


def _safe_text(value: str) -> str:
    forbidden_categories = {"Cc", "Cf", "Cs", "Zl", "Zp"}
    if any(
        ord(character) == 127 or unicodedata.category(character) in forbidden_categories
        for character in value
    ):
        raise ValueError("Текст не должен содержать управляющие символы")
    if _contains_credential_canary(value):
        raise ValueError("Текст не должен содержать credential/DSN canary")
    return value


def _canonical_fingerprint_input(value: object) -> object:
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _LOWER_HEX_DIGITS for character in value)
    ):
        return f"sha256:{value}"
    return value


IdentifierStr = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=255),
    AfterValidator(_safe_text),
]
ParserIdentifierStr = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$",
    ),
    AfterValidator(_safe_text),
]
VersionStr = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        min_length=1,
        max_length=128,
    ),
    AfterValidator(_safe_text),
]
SchemaVersionStr = Annotated[
    str,
    StringConstraints(
        strict=True,
        strip_whitespace=True,
        pattern=r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$",
    ),
]
FingerprintStr = Annotated[
    str,
    BeforeValidator(_canonical_fingerprint_input),
    StringConstraints(strict=True, pattern=r"^sha256:[0-9a-f]{64}$"),
]
IssueCodeStr = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9_.-]{0,254}$"),
]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]
_OpaqueSourceIdentifier = IdentifierStr


def _finite_decimal_input(value: object) -> object:
    if isinstance(value, bool | float):
        raise ValueError("Decimal принимается только без двоичного float и bool")
    return value


def _finite_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Decimal должен быть конечным")
    return value


def _confidence(value: Decimal) -> Decimal:
    if value < 0 or value > 1:
        raise ValueError("Confidence должен находиться в диапазоне [0, 1]")
    return value


def _finite_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("Float должен быть конечным")
    return value


def _exact_finite_float_input(value: object) -> object:
    if type(value) is not float:
        raise ValueError("Number scalar принимает только binary float")
    return value


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Datetime должен содержать часовой пояс")
    return value.astimezone(UTC)


def _datetime_input(value: object) -> object:
    if type(value) is datetime:
        return value
    if type(value) is str and _DATETIME_TEXT.fullmatch(value) is not None:
        return value
    raise ValueError("Tagged datetime принимает только timezone-aware ISO datetime")


def _date_input(value: object) -> object:
    if type(value) is date:
        return value
    if type(value) is str and _DATE_TEXT.fullmatch(value) is not None:
        return value
    raise ValueError("Tagged date принимает только ISO calendar date")


FiniteDecimal = Annotated[
    Decimal,
    BeforeValidator(_finite_decimal_input),
    AfterValidator(_finite_decimal),
]
ConfidenceDecimal = Annotated[FiniteDecimal, AfterValidator(_confidence)]
FiniteFloat = Annotated[StrictFloat, AfterValidator(_finite_float)]
_ExactFiniteFloat = Annotated[
    StrictFloat,
    BeforeValidator(_exact_finite_float_input),
    AfterValidator(_finite_float),
]
UtcDateTime = Annotated[
    datetime,
    BeforeValidator(_datetime_input),
    AfterValidator(_utc_datetime),
]
CalendarDate = Annotated[date, BeforeValidator(_date_input)]


class SemanticParsingMode(StrEnum):
    """Режим формирования семантического ParsePlan."""

    DETERMINISTIC = "deterministic"
    LLM_ASSISTED = "llm_assisted"
    LLM_FIRST = "llm_first"


class DataClassification(StrEnum):
    """Закрытая классификация данных перед security и LLM boundaries."""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


def _exact_data_classification_input(value: object) -> object:
    if isinstance(value, DataClassification) or type(value) is str:
        return value
    raise ValueError("Data classification принимает только точное wire-значение")


def _redact_union_validation_input(
    value: object,
    handler: ValidatorFunctionWrapHandler,
) -> object:
    try:
        return handler(value)
    except ValidationError as error:
        raise _redacted_validation_error("DiscriminatedUnion", error) from None


class ParsePlanKind(StrEnum):
    """Закрытые варианты ParsePlan."""

    TABULAR = "tabular"
    TREE = "tree"
    LOG = "log"
    DOCUMENT = "document"


class ValidationDecision(StrEnum):
    """Решение проверки декларативного плана."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


class IssueSeverity(StrEnum):
    """Стабильная серьёзность диагностической проблемы."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class LoadOperation(StrEnum):
    """Разрешённая декларативная операция загрузки."""

    INSERT_ONLY = "insert_only"
    UPSERT = "upsert"


class ErrorPolicy(StrEnum):
    """Политика обработки ошибочных записей."""

    ATOMIC = "atomic"
    QUARANTINE_INVALID = "quarantine_invalid"
    BEST_EFFORT = "best_effort"


class TransactionOutcome(StrEnum):
    """Стабильный статус транзакции загрузки."""

    NOT_STARTED = "not_started"
    DRY_RUN = "dry_run"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class PipelineStatus(StrEnum):
    """Стабильные wire-статусы pipeline v2."""

    CREATED = "CREATED"
    SOURCE_PROBING = "SOURCE_PROBING"
    TECHNICAL_PARSING = "TECHNICAL_PARSING"
    STRUCTURE_PROFILING = "STRUCTURE_PROFILING"
    STRUCTURE_ANALYZING = "STRUCTURE_ANALYZING"
    PARSE_PLAN_CREATED = "PARSE_PLAN_CREATED"
    PARSE_PLAN_VALIDATING = "PARSE_PLAN_VALIDATING"
    SEMANTIC_PARSING = "SEMANTIC_PARSING"
    NORMALIZED_DATA_PROFILING = "NORMALIZED_DATA_PROFILING"
    DATABASE_INSPECTING = "DATABASE_INSPECTING"
    MAPPING = "MAPPING"
    MAPPING_PLAN_CREATED = "MAPPING_PLAN_CREATED"
    MAPPING_PLAN_VALIDATING = "MAPPING_PLAN_VALIDATING"
    NORMALIZING = "NORMALIZING"
    VALIDATING = "VALIDATING"
    STAGING = "STAGING"
    LOADING = "LOADING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    REJECTED_SECURITY = "REJECTED_SECURITY"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BuiltInErrorCode(StrEnum):
    """Встроенные стабильные error codes; адаптеры могут расширять словарь."""

    SDK_OPERATION_NOT_IMPLEMENTED = "SDK_OPERATION_NOT_IMPLEMENTED"
    SYNC_API_IN_ASYNC_CONTEXT = "SYNC_API_IN_ASYNC_CONTEXT"
    SECURITY_SANDBOX_REQUIRED = "SECURITY_SANDBOX_REQUIRED"
    PARSER_NO_TEXT_LAYER = "PARSER_NO_TEXT_LAYER"
    SOURCE_FINGERPRINT_MISMATCH = "SOURCE_FINGERPRINT_MISMATCH"
    SOURCE_PATH_NOT_ALLOWED = "SOURCE_PATH_NOT_ALLOWED"
    SOURCE_SNAPSHOT_EXPIRED = "SOURCE_SNAPSHOT_EXPIRED"
    DATABASE_FINGERPRINT_MISMATCH = "DATABASE_FINGERPRINT_MISMATCH"
    DATABASE_TARGET_MISMATCH = "DATABASE_TARGET_MISMATCH"
    AUDIT_DURABILITY_REQUIRED = "AUDIT_DURABILITY_REQUIRED"
    PROCESSING_TIMEOUT = "PROCESSING_TIMEOUT"
    DDL_FORBIDDEN = "DDL_FORBIDDEN"
    TARGET_NOT_ALLOWED = "TARGET_NOT_ALLOWED"
    CONTRACT_VERSION_UNSUPPORTED = "CONTRACT_VERSION_UNSUPPORTED"
    PROVENANCE_INVALID = "PROVENANCE_INVALID"
    PARSE_PLAN_INVALID = "PARSE_PLAN_INVALID"
    MAPPING_PLAN_INVALID = "MAPPING_PLAN_INVALID"
    PROMPT_INJECTION_DETECTED = "PROMPT_INJECTION_DETECTED"
    SECURITY_LIMIT_EXCEEDED = "SECURITY_LIMIT_EXCEEDED"
    SECURITY_INPUT_REJECTED = "SECURITY_INPUT_REJECTED"
    LLM_DATA_ROUTING_FORBIDDEN = "LLM_DATA_ROUTING_FORBIDDEN"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    PARSER_INVALID_ADAPTER = "PARSER_INVALID_ADAPTER"
    PARSER_DUPLICATE_REGISTRATION = "PARSER_DUPLICATE_REGISTRATION"
    PARSER_REGISTRY_FROZEN = "PARSER_REGISTRY_FROZEN"
    PARSER_SESSION_CLOSED = "PARSER_SESSION_CLOSED"
    PARSER_NOT_FOUND = "PARSER_NOT_FOUND"
    PARSER_UNSUPPORTED_FORMAT = "PARSER_UNSUPPORTED_FORMAT"
    PARSER_DEPENDENCY_UNAVAILABLE = "PARSER_DEPENDENCY_UNAVAILABLE"
    PARSER_MALFORMED_INPUT = "PARSER_MALFORMED_INPUT"
    PARSER_UNSUPPORTED_FEATURE = "PARSER_UNSUPPORTED_FEATURE"
    PARSER_ENCODING_UNSUPPORTED = "PARSER_ENCODING_UNSUPPORTED"
    PARSER_PROBE_FAILED = "PARSER_PROBE_FAILED"
    PARSER_PROBE_INVALID = "PARSER_PROBE_INVALID"
    PARSER_FORMAT_CONFLICT = "PARSER_FORMAT_CONFLICT"
    PARSER_PLUGIN_METADATA_INVALID = "PARSER_PLUGIN_METADATA_INVALID"
    PARSER_PLUGIN_DISCOVERY_FAILED = "PARSER_PLUGIN_DISCOVERY_FAILED"
    PARSER_OUTPUT_INVALID = "PARSER_OUTPUT_INVALID"


class BuiltInIssueCode(StrEnum):
    """Стартовый additive vocabulary встроенных issue codes."""

    INVALID_SOURCE_LOCATION = "INVALID_SOURCE_LOCATION"
    DUPLICATE_ARTIFACT_ID = "DUPLICATE_ARTIFACT_ID"
    INVALID_BATCH_SEQUENCE = "INVALID_BATCH_SEQUENCE"
    PROVENANCE_REFERENCE_MISSING = "PROVENANCE_REFERENCE_MISSING"
    UPSTREAM_FINGERPRINT_MISMATCH = "UPSTREAM_FINGERPRINT_MISMATCH"
    UNKNOWN_PLAN_OPERATOR = "UNKNOWN_PLAN_OPERATOR"
    PLAN_CONTAINS_EXECUTABLE_CONTENT = "PLAN_CONTAINS_EXECUTABLE_CONTENT"
    AMBIGUOUS_STRUCTURE = "AMBIGUOUS_STRUCTURE"
    UNSUPPORTED_CONTRACT_VERSION = "UNSUPPORTED_CONTRACT_VERSION"
    PARSER_DECLARED_MIME_MISMATCH = "PARSER_DECLARED_MIME_MISMATCH"
    PARSER_EXTENSION_MISMATCH = "PARSER_EXTENSION_MISMATCH"


class PhysicalObjectKind(StrEnum):
    """Закрытые виды объектов, на которые ссылается physical provenance."""

    LINE = "line"
    BLOCK = "block"
    TABLE = "table"
    CELL = "cell"
    TREE_NODE = "tree_node"
    VALUE = "value"
    EXTENSION = "extension"


_PHYSICAL_LINE_ID = re.compile(rf"^line-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_BLOCK_ID = re.compile(rf"^block-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_TABLE_ID = re.compile(rf"^table-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_CELL_ID = re.compile(rf"^cell-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_TREE_NODE_ID = re.compile(rf"^node-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_VALUE_ID = re.compile(rf"^value-{_GENERATED_ID_SUFFIX}$")
_PHYSICAL_EXTENSION_ID = re.compile(rf"^extension-{_GENERATED_ID_SUFFIX}$")


def _validate_generated_extraction_id(extraction_id: str) -> None:
    if _EXTRACTION_ID.fullmatch(extraction_id) is None:
        raise ValueError("extraction_id не является parser-generated identifier")


def _validate_generated_physical_local_id(
    kind: PhysicalObjectKind,
    local_id: str,
) -> None:
    if kind is PhysicalObjectKind.LINE:
        pattern = _PHYSICAL_LINE_ID
    elif kind is PhysicalObjectKind.BLOCK:
        pattern = _PHYSICAL_BLOCK_ID
    elif kind is PhysicalObjectKind.TABLE:
        pattern = _PHYSICAL_TABLE_ID
    elif kind is PhysicalObjectKind.CELL:
        pattern = _PHYSICAL_CELL_ID
    elif kind is PhysicalObjectKind.TREE_NODE:
        pattern = _PHYSICAL_TREE_NODE_ID
    elif kind is PhysicalObjectKind.VALUE:
        pattern = _PHYSICAL_VALUE_ID
    elif kind is PhysicalObjectKind.EXTENSION:
        pattern = _PHYSICAL_EXTENSION_ID
    else:
        raise ValueError("Неизвестный physical object kind")
    if pattern.fullmatch(local_id) is None:
        raise ValueError("local_id не соответствует physical object kind")


class ProducerMetadata(FrozenContract):
    """Версии компонента, создавшего persisted artifact."""

    component_id: IdentifierStr
    component_version: VersionStr
    sdk_version: VersionStr
    provider_id: IdentifierStr | None = None
    model_id: IdentifierStr | None = None
    prompt_fingerprint: FingerprintStr | None = None
    generation_version: VersionStr | None = None

    @model_validator(mode="after")
    def _validate_provider_metadata(self) -> Self:
        if (self.provider_id is None) != (self.model_id is None):
            raise ValueError("provider_id и model_id указываются вместе")
        if self.provider_id is None and (
            self.prompt_fingerprint is not None or self.generation_version is not None
        ):
            raise ValueError("LLM metadata требует provider_id и model_id")
        return self


class SourceArtifactRef(FrozenContract):
    """Проверяемая идентичность immutable snapshot источника."""

    artifact_id: IdentifierStr
    source_fingerprint: FingerprintStr


class PhysicalSourceRef(FrozenContract):
    """Квалифицированная ссылка на физический объект extraction run."""

    extraction_id: _OpaqueSourceIdentifier
    batch_index: NonNegativeInt
    kind: PhysicalObjectKind
    local_id: _OpaqueSourceIdentifier

    @model_validator(mode="after")
    def _validate_generated_identifiers(self) -> Self:
        _validate_generated_extraction_id(self.extraction_id)
        _validate_generated_physical_local_id(self.kind, self.local_id)
        return self


class BatchFingerprint(FrozenContract):
    """Связь индекса batch с его собственным fingerprint."""

    batch_index: NonNegativeInt
    batch_fingerprint: FingerprintStr


class ValidationIssue(FrozenContract):
    """Безопасная типизированная проблема validation/report boundary."""

    code: IssueCodeStr
    severity: IssueSeverity
    message_key: IssueCodeStr
    source_refs: tuple[PhysicalSourceRef, ...] = ()


class StringScalar(FrozenContract):
    """Tagged строковое значение."""

    kind: Literal["string"] = "string"
    value: StrictStr


class IntegerScalar(FrozenContract):
    """Tagged целое значение без смешения с bool."""

    kind: Literal["integer"] = "integer"
    value: StrictInt


class NumberScalar(FrozenContract):
    """Tagged конечное двоичное число."""

    kind: Literal["number"] = "number"
    value: _ExactFiniteFloat


class DecimalScalar(FrozenContract):
    """Tagged точное десятичное значение."""

    kind: Literal["decimal"] = "decimal"
    value: FiniteDecimal


class BooleanScalar(FrozenContract):
    """Tagged логическое значение."""

    kind: Literal["boolean"] = "boolean"
    value: StrictBool


class DateScalar(FrozenContract):
    """Tagged календарная дата."""

    kind: Literal["date"] = "date"
    value: CalendarDate


class DateTimeScalar(FrozenContract):
    """Tagged-значение datetime, нормализованное в UTC."""

    kind: Literal["datetime"] = "datetime"
    value: UtcDateTime


class BytesScalar(FrozenContract):
    """Tagged bytes, допустимый только в physical raw model."""

    kind: Literal["bytes"] = "bytes"
    value: StrictBytes


class NullScalar(FrozenContract):
    """Tagged null без потери типа при round-trip."""

    kind: Literal["null"] = "null"
    value: None = None


type RawScalar = Annotated[
    StringScalar
    | IntegerScalar
    | NumberScalar
    | DecimalScalar
    | BooleanScalar
    | DateScalar
    | DateTimeScalar
    | BytesScalar
    | NullScalar,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]
type NormalizedScalar = Annotated[
    StringScalar
    | IntegerScalar
    | NumberScalar
    | DecimalScalar
    | BooleanScalar
    | DateScalar
    | DateTimeScalar
    | NullScalar,
    Field(discriminator="kind"),
    WrapValidator(_redact_union_validation_input),
]
