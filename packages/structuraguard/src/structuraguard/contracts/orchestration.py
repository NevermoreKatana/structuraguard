"""Результат SDK: композиция проверенных component reports и частичных outcomes."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import FrozenContract
from .audit import AuditHead
from .common import FingerprintStr, IdentifierStr, PipelineStatus, TransactionOutcome
from .database import DatabaseCatalog
from .deterministic_mapping import DeterministicMappingResult
from .injection import InjectionSummary
from .llm import LLMCallRecord
from .loading import DryRunExecutionPlan, PostgreSQLLoadResult
from .mapping import MappingPlan, MappingPlanValidationResult
from .mapping_validation import MappingPlanInputReport
from .parsing import ParsePlan, ParsePlanValidationResult, StructureProfile
from .privacy import PrivacySummary
from .profiling import NormalizedDataProfile
from .provenance import DetailedValidationReport
from .reports import AuditEvent, LoadReport, SecurityReport, SemanticParseReport
from .security import ResourceAuditEvent
from .semantic_mapping import SemanticMappingResult
from .source import ExtractedDatasetManifest, ProbeResult, SourceArtifact


class PipelineFailure(FrozenContract):
    """Ошибка stage с первичным code и безопасной цепочкой cause_codes.

    Текст исходных исключений и raw payload не сохраняются. Это evidence ошибки,
    а не traceback и не доказательство rollback; создание DTO не выполняет I/O.
    """

    code: IdentifierStr
    stage: PipelineStatus
    cause_codes: Annotated[tuple[IdentifierStr, ...], Field(max_length=16)] = ()


class SDKSecurityReport(FrozenContract):
    """Сводка security policy и фактически выполненных проверок запуска.

    policy_fingerprint связывает политику; resources, privacy, injection и scans
    сохраняют component reports. Пустые коллекции означают отсутствие evidence,
    а не успешную проверку. Агрегат не подменяет approval scanner для точного
    request и не разрешает egress/load. Создание DTO не выполняет I/O.
    """

    policy_fingerprint: FingerprintStr
    resources: tuple[ResourceAuditEvent, ...] = ()
    privacy: tuple[PrivacySummary, ...] = ()
    injection: tuple[InjectionSummary, ...] = ()
    scans: tuple[SecurityReport, ...] = ()


class IngestResult(FrozenContract):
    """Полный либо частичный результат SDK schema 1.0.0 без полномочий на replay.

    run_id/status описывают запуск; fingerprints связывают source, extraction,
    ParsePlan, normalized data, database и MappingPlan. Неисполненные stages
    имеют None вместо выдуманного report; provider_metadata содержит фактические
    attempts. transaction_outcome отделяет commit/rollback/unknown от status;
    dry_run не допускает committed evidence. audit_references связывают подписи,
    но не гарантируют доставку audit внешнему получателю.

    Несовместимые bindings при создании/десериализации дают Pydantic ValidationError.
    Проверка envelope не заменяет validation планов и DB policy. Создание DTO
    не выполняет I/O. Полный JSON может содержать PII; для logs есть safe_summary.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: IdentifierStr
    status: PipelineStatus
    dry_run: bool
    transaction_outcome: TransactionOutcome = TransactionOutcome.NOT_STARTED
    source_fingerprint: FingerprintStr | None = None
    extraction_fingerprint: FingerprintStr | None = None
    parse_plan_fingerprint: FingerprintStr | None = None
    normalized_fingerprint: FingerprintStr | None = None
    database_fingerprint: FingerprintStr | None = None
    mapping_plan_fingerprint: FingerprintStr | None = None
    source_report: SourceArtifact | None = Field(default=None, repr=False)
    probe: ProbeResult | None = Field(default=None, repr=False)
    extraction: ExtractedDatasetManifest | None = Field(default=None, repr=False)
    structure_profile: StructureProfile | None = Field(default=None, repr=False)
    parse_plan: ParsePlan | None = Field(default=None, repr=False)
    parse_validation: ParsePlanValidationResult | None = Field(default=None, repr=False)
    semantic_parse_report: SemanticParseReport | None = Field(default=None, repr=False)
    normalized_profile: NormalizedDataProfile | None = Field(default=None, repr=False)
    database_report: DatabaseCatalog | None = Field(default=None, repr=False)
    mapping_plan: MappingPlan | None = Field(default=None, repr=False)
    candidates: DeterministicMappingResult | None = Field(default=None, repr=False)
    semantic_mapping: SemanticMappingResult | None = Field(default=None, repr=False)
    mapping_validation: MappingPlanValidationResult | MappingPlanInputReport | None = (
        Field(default=None, repr=False)
    )
    validation_report: DetailedValidationReport | None = Field(default=None, repr=False)
    load_report: LoadReport | None = Field(default=None, repr=False)
    load_result: PostgreSQLLoadResult | None = Field(default=None, repr=False)
    dry_run_plan: DryRunExecutionPlan | None = Field(default=None, repr=False)
    provider_metadata: tuple[LLMCallRecord, ...] = ()
    security_report: SDKSecurityReport
    audit_events: tuple[AuditEvent, ...] = ()
    audit_references: tuple[AuditHead, ...] = ()
    errors: tuple[PipelineFailure, ...] = ()
    warnings: tuple[IdentifierStr, ...] = ()

    @model_validator(mode="after")
    def bindings(self) -> Self:
        """Проверить bindings и вернуть self без I/O.

        Несовпавший fingerprint, чужой audit run либо committed dry-run даёт
        ValueError; при создании модели Pydantic включает его в ValidationError.
        """
        if (
            self.source_report
            and self.source_fingerprint != self.source_report.source_fingerprint
        ):
            raise ValueError("Source fingerprint mismatch")
        if (
            self.parse_plan
            and self.parse_plan_fingerprint != self.parse_plan.fingerprint
        ):
            raise ValueError("ParsePlan fingerprint mismatch")
        if (
            self.mapping_plan
            and self.mapping_plan_fingerprint != self.mapping_plan.fingerprint
        ):
            raise ValueError("MappingPlan fingerprint mismatch")
        if (
            self.database_report
            and self.database_fingerprint != self.database_report.database_fingerprint
        ):
            raise ValueError("Database fingerprint mismatch")
        if self.dry_run and (
            self.load_result or self.transaction_outcome is TransactionOutcome.COMMITTED
        ):
            raise ValueError("Dry-run не содержит committed writes")
        if (
            self.extraction
            and self.extraction_fingerprint != self.extraction.extraction_fingerprint
        ):
            raise ValueError("Extraction fingerprint mismatch")
        if (
            self.normalized_profile
            and self.normalized_fingerprint
            != self.normalized_profile.normalized_manifest_fingerprint
        ):
            raise ValueError("Normalized fingerprint mismatch")
        if self.load_report:
            for name in (
                "source_fingerprint",
                "extraction_fingerprint",
                "parse_plan_fingerprint",
                "normalized_fingerprint",
                "mapping_plan_fingerprint",
                "database_fingerprint",
                "transaction_outcome",
                "dry_run",
            ):
                if getattr(self, name) != getattr(self.load_report, name):
                    raise ValueError("LoadReport binding mismatch")
        if any(event.run_id != self.run_id for event in self.audit_events):
            raise ValueError("Audit run binding mismatch")
        for evidence in (self.load_result, self.dry_run_plan):
            if evidence and (
                evidence.database_fingerprint,
                evidence.normalized_fingerprint,
                evidence.mapping_fingerprint,
            ) != (
                self.database_fingerprint,
                self.normalized_fingerprint,
                self.mapping_plan_fingerprint,
            ):
                raise ValueError("Execution evidence binding mismatch")
        return self

    def safe_summary(self) -> dict[str, str | int | bool]:
        """Вернуть новую сводку status/dry_run и counts без raw evidence и I/O.

        Plans, labels, значения и тексты исключений исключены. loaded_records=0
        при отсутствии load_report не означает успешный dry-run: учитывайте status.
        """
        return {
            "status": self.status.value,
            "dry_run": self.dry_run,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "loaded_records": self.load_report.loaded_records
            if self.load_report
            else 0,
        }
