"""Контракты dry-run/load, явная idempotency и audit metadata без исходных значений.

DTO проверяются Pydantic без I/O; неверная структура даёт ValidationError.
Они не удостоверяют подлинность artifacts и не разрешают запись сами по себе.
Полный JSON чувствителен, даже если repr скрыт; для logs используйте safe_summary.
"""

import re
from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ._base import FrozenContract, canonical_sha256_value
from .audit import AuditHead
from .common import (
    FingerprintStr,
    IdentifierStr,
    NonNegativeInt,
    PositiveInt,
    UtcDateTime,
)
from .constraint_validation import CheckRuleBinding, ConstraintReadPolicy
from .database import CatalogColumnRef, StagingContext
from .database_policy import PostgreSQLAuditPolicy
from .mapping import MappingPlan
from .mapping_validation import MappingValidationPolicy
from .normalized import NormalizedBatch


class _Sensitive(FrozenContract):
    def __repr_args__(self) -> Iterable[tuple[str | None, object]]:
        return ()


class DryRunRequest(_Sensitive):
    """Полный normalized snapshot для прогноза и повторной проверки перед записью.

    Args:
        batches: Tuple NormalizedBatch с полным EOF manifest, в исходном порядке.
        mapping: MappingPlan, связанный с теми же source/catalog fingerprints.

    Создание DTO проверяет структуру через Pydantic, без I/O. Lineage, бюджеты
    и актуальная схема проверяются planner/loader повторно. Полный JSON содержит
    исходные значения; его нельзя логировать. Persistent staging не читается.
    """

    batches: tuple[NormalizedBatch, ...] = Field(min_length=1, max_length=1001)
    mapping: MappingPlan


class DryRunPolicy(_Sensitive):
    """Trusted policy отдельно от недоверенных данных и MappingPlan.

    writer_principal проверяется по grants в той же БД; его credentials не нужны.
    read_policy явно разрешает SELECT ключей через отдельный inspector principal.
    Atomic по умолчанию; quarantine не разрешает неизвестную семантику.
    """

    writer_principal: IdentifierStr
    mapping_policy: MappingValidationPolicy
    read_policy: ConstraintReadPolicy
    error_policy: Literal["atomic", "quarantine_invalid"] = "atomic"
    checks: tuple[CheckRuleBinding, ...] = Field(default=(), max_length=512)


class ExecutionStep(_Sensitive):
    """Одна target row, связанная с source record/entity/value IDs без значений."""

    unit_id: IdentifierStr
    record_id: IdentifierStr
    entity_id: IdentifierStr
    table_id: IdentifierStr
    value_ids: tuple[IdentifierStr, ...] = Field(max_length=512)
    column_ids: tuple[IdentifierStr, ...] = Field(max_length=512)
    identity_column_ids: tuple[IdentifierStr, ...] = Field(max_length=64)
    update_column_ids: tuple[IdentifierStr, ...] = Field(max_length=512)
    dependencies: tuple[IdentifierStr, ...] = Field(max_length=10000)
    action: Literal["insert", "update", "skip", "quarantine"]
    codes: tuple[IdentifierStr, ...] = Field(default=(), max_length=1024)


class DryRunExecutionPlan(_Sensitive):
    """Прогноз одного snapshot, а не разрешение на commit или резервирование keys.

    Полный артефакт содержит чувствительные identifiers/fingerprints. Для logs
    предназначен только safe_summary(); SQL, параметры и DSN отсутствуют в DTO.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    dry_run: Literal[True] = True
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    target_policy_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    mapping_fingerprint: FingerprintStr
    mapping_validation_fingerprint: FingerprintStr
    projection_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    validation_fingerprint: FingerprintStr
    read_snapshot_fingerprint: FingerprintStr
    table_order: tuple[IdentifierStr, ...] = Field(max_length=256)
    steps: tuple[ExecutionStep, ...] = Field(max_length=10000)
    blockers: tuple[IdentifierStr, ...] = Field(default=(), max_length=1024)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        """Проверить уникальность units и порядок dependencies при валидации DTO."""
        seen: set[str] = set()
        for step in self.steps:
            if step.unit_id in seen or not set(step.dependencies) <= seen:
                raise ValueError("Повтор unit ID либо нарушен порядок зависимостей")
            if step.table_id not in self.table_order:
                raise ValueError("Таблица отсутствует в load order")
            seen.add(step.unit_id)
        return self

    @property
    def ready(self) -> bool:
        """Все проверки поддержанного scope завершены в текущем read snapshot."""
        return not self.blockers

    @property
    def planned_inserts(self) -> int:
        """Число target units с прогнозом INSERT, без изменения данных."""
        return sum(s.action == "insert" for s in self.steps)

    @property
    def planned_updates(self) -> int:
        """Число target units с прогнозом UPDATE, включая равные прежним значения."""
        return sum(s.action == "update" for s in self.steps)

    @property
    def planned_skips(self) -> int:
        """Число существующих upsert identities без mutable columns."""
        return sum(s.action == "skip" for s in self.steps)

    @property
    def planned_quarantine(self) -> int:
        """Число исключённых target units; persistent quarantine не записывается."""
        return sum(s.action == "quarantine" for s in self.steps)

    @property
    def fingerprint(self) -> str:
        """Связать вход, решения и политики; clock/run ID не участвуют."""
        return canonical_sha256_value(self.canonical_json())

    def safe_summary(self) -> dict[str, int | bool]:
        """Выдать только counts/ready; identifiers и hashes остаются в артефакте."""
        return {
            "dry_run": True,
            "ready": self.ready,
            "planned_inserts": self.planned_inserts,
            "planned_updates": self.planned_updates,
            "planned_skips": self.planned_skips,
            "planned_quarantine": self.planned_quarantine,
            "blocker_count": len(self.blockers),
        }


class ServerValuePermission(_Sensitive):
    """Trusted разрешение вычислить существующий non-key default/generated value.

    Hash связывает точную ColumnInspectionMetadata; это разрешение владельца,
    а не доказательство безопасности выражения. SQL текст policy не принимает.
    """

    column: CatalogColumnRef
    metadata_fingerprint: FingerprintStr
    evaluation_allowed: Literal[True]


class LoadLedgerPolicy(_Sensitive):
    """Trusted namespace и отдельная заранее созданная schema той же target DB.

    Marker хранится бессрочно; автоматическая очистка tombstones запрещена.
    Namespace не заменяет PostgreSQL grants между недоверенными tenants.
    """

    namespace: IdentifierStr
    schema_name: IdentifierStr = "sg_staging_load_main"
    max_receipt_bytes: Annotated[PositiveInt, Field(le=16777216)] = 4194304

    @model_validator(mode="after")
    def dedicated_schema(self) -> Self:
        """Отклонить ledger вне выделенной schema через Pydantic ValidationError."""
        if re.fullmatch(r"sg_staging_load_[a-z0-9_]{1,32}", self.schema_name) is None:
            raise ValueError("Ledger требует выделенную sg_staging_load_* schema")
        return self


class PostgreSQLLoadPolicy(_Sensitive):
    """Доверенная policy записи; atomic по умолчанию, quarantine требует ledger.

    Args:
        preflight: M11/read policy, writer principal, CHECK bindings и error mode.
        write_tables: Явные пары (schema, table) внутри mapping allowlist.
        batch_size: Максимум target units в SQL statement.
        max_parameters: Верхняя граница bind parameters одного statement.
        max_batch_bytes: Бюджет значений одного SQL batch.
        server_values: Явные разрешения вычислять существующие non-key defaults.
        ledger: Policy заранее установленного журнала той же DB либо None.
        audit: PostgreSQLAuditPolicy заранее установленной audit schema либо None.
            Signed event и delivery intent записываются в target transaction;
            central require_signed_audit не допускает None. Signer передаётся loader.

    Raises:
        pydantic.ValidationError: Неверные limits, повтор tables/permissions,
            расширение allowlist либо quarantine без ledger.

    Конструктор не выполняет I/O. Policy принадлежит владельцу приложения;
    SQL и разрешения из недоверенного MappingPlan не принимаются.
    """

    preflight: DryRunPolicy
    write_tables: tuple[tuple[IdentifierStr, IdentifierStr], ...] = Field(
        min_length=1, max_length=256
    )
    batch_size: Annotated[PositiveInt, Field(le=1000)] = 100
    max_parameters: Annotated[PositiveInt, Field(le=30000)] = 30000
    max_batch_bytes: Annotated[PositiveInt, Field(le=16777216)] = 1048576
    server_values: tuple[ServerValuePermission, ...] = Field(default=(), max_length=512)
    ledger: LoadLedgerPolicy | None = None
    audit: PostgreSQLAuditPolicy | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def admission(self) -> Self:
        """Проверить allowlist, ledger и permissions до любого обращения к БД."""
        if len(set(self.write_tables)) != len(self.write_tables) or not set(
            self.write_tables
        ) <= set(self.preflight.mapping_policy.allow_tables):
            raise ValueError(
                "Write tables должны быть уникальны и входить в trusted allowlist"
            )
        if self.preflight.error_policy == "quarantine_invalid" and self.ledger is None:
            raise ValueError("Quarantine требует durable ledger той же PostgreSQL DB")
        if len({p.column for p in self.server_values}) != len(self.server_values):
            raise ValueError("Повтор разрешения server value")
        return self


class LoadRequest(_Sensitive):
    """Snapshot и staging binding одного вызова loader; полный JSON чувствителен.

    Args:
        snapshot: Полные batches и соответствующий MappingPlan.
        staging_context: Исходный context SEALED run.
        staging_revision: Revision после seal.
        idempotency_key: Непрозрачный key; обязателен с ledger и запрещён без него.

    Pydantic проверяет форму без I/O; loader сверяет staging, hashes и актуальную
    схему. Key скрыт в repr, но присутствует в полной JSON-сериализации.
    """

    snapshot: DryRunRequest
    staging_context: StagingContext
    staging_revision: NonNegativeInt
    idempotency_key: IdentifierStr | None = Field(default=None, repr=False)


class PostgreSQLLoadResult(_Sensitive):
    """Результат подтверждённого COMMIT либо восстановления committed marker.

    inserted/updated/skipped/quarantined считают target units; loaded_records
    и rejected_records — исходные records. generated_at — UTC начало исходного
    attempt. При replayed=True сохраняются исходные counts/run_id, а attempt_run_id
    указывает текущий вызов. Повторной записи эти counts не означают.

    warnings сообщают о сбое cleanup/finalize после известного COMMIT. Полный JSON
    с identifiers/fingerprints чувствителен; для logs предназначен safe_summary.
    Создание DTO не выполняет I/O и само по себе не доказывает DB commit.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    dry_run: Literal[False] = False
    transaction_outcome: Literal["committed"] = "committed"
    run_id: IdentifierStr
    target_id: IdentifierStr
    database_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    mapping_fingerprint: FingerprintStr
    execution_plan_fingerprint: FingerprintStr
    sealed_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    inserted: NonNegativeInt
    updated: NonNegativeInt
    skipped: NonNegativeInt
    quarantined: NonNegativeInt = 0
    loaded_records: NonNegativeInt = 0
    rejected_records: NonNegativeInt = 0
    replayed: bool = False
    attempt_run_id: IdentifierStr | None = None
    generated_at: UtcDateTime
    warnings: tuple[IdentifierStr, ...] = Field(default=(), max_length=16)
    audit_head: AuditHead | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    def safe_summary(self) -> dict[str, int | bool]:
        """Вернуть counts и признаки результата, пригодные для logs.

        При replay inserted/updated равны нулю; skipped/quarantined остаются
        счётчиками исходного исполнения. SQL, values, IDs, keys и hashes отсутствуют.
        Метод не выполняет I/O и не изменяет исходный result.
        """
        return {
            "committed": True,
            "replayed": self.replayed,
            "inserted": 0 if self.replayed else self.inserted,
            "updated": 0 if self.replayed else self.updated,
            "skipped": self.skipped,
            "quarantined": self.quarantined,
            "warning_count": len(self.warnings),
        }


class LoadQuarantineReference(_Sensitive):
    """Hash ссылки на immutable staging unit; raw values/driver text отсутствуют."""

    run_hash: FingerprintStr
    record_hash: FingerprintStr
    unit_hash: FingerprintStr
    table_hash: FingerprintStr
    codes: tuple[IdentifierStr, ...] = Field(min_length=1, max_length=1024)


class LoadAuditMetadata(_Sensitive):
    """Redacted committed event, атомарный с target и idempotency marker."""

    outcome: Literal["committed"] = "committed"
    mode: Literal["atomic", "quarantine_invalid"]
    run_hash: FingerprintStr
    source_fingerprint: FingerprintStr
    mapping_fingerprint: FingerprintStr
    normalized_fingerprint: FingerprintStr
    database_fingerprint: FingerprintStr
    policy_fingerprint: FingerprintStr
    binding_fingerprint: FingerprintStr
    inserted: NonNegativeInt
    updated: NonNegativeInt
    skipped: NonNegativeInt
    quarantined: NonNegativeInt
    loaded_records: NonNegativeInt
    rejected_records: NonNegativeInt
    generated_at: UtcDateTime
