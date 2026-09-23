"""Trusted dependencies и factories одного run без глобальных registries."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from structuraguard.contracts.business_rules import BusinessRuleSet
from structuraguard.contracts.database import DatabaseInspectionRequest
from structuraguard.contracts.deterministic_mapping import DeterministicMappingOptions
from structuraguard.contracts.injection import InjectionPolicy
from structuraguard.contracts.llm import LLMRoutingPolicy
from structuraguard.contracts.loading import (
    DryRunExecutionPlan,
    DryRunPolicy,
    DryRunRequest,
    LoadRequest,
    PostgreSQLLoadResult,
)
from structuraguard.contracts.privacy import DetectionPolicy
from structuraguard.contracts.provenance import ProvenancePolicy
from structuraguard.contracts.reports import AuditEvent
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.contracts.semantic import ParsingPolicy
from structuraguard.contracts.semantic_catalog import DatabaseSemanticCatalog
from structuraguard.contracts.semantic_mapping import SemanticMappingOptions
from structuraguard.contracts.staging import StagingArtifactReference
from structuraguard.ports.database import DatabaseAdapter
from structuraguard.ports.llm import LLMProvider
from structuraguard.ports.security import SecurityScanner
from structuraguard.ports.stores import RunStagingStore
from structuraguard.ports.validation import ConstraintReader
from structuraguard.security.audit import AuditChain
from structuraguard.security.session import SecuritySession


def utc_now() -> datetime:
    """UTC clock вызывается только в операции."""
    return datetime.now(UTC)


class DryRunPlanner(Protocol):
    """Порт read-only прогноза; реализацию и DB resources предоставляет host."""

    async def plan(self, request: DryRunRequest) -> DryRunExecutionPlan:
        """Проверить request и вернуть прогноз без target/staging writes.

        Реализация соблюдает общий resource guard; ошибки adapter и отмена
        передаются coordinator. DDL и исполнение SQL из входа запрещены.
        """
        ...


class Loader(Protocol):
    """Порт транзакционной загрузки с явным подтверждением исхода записи."""

    async def execute(self, request: LoadRequest) -> PostgreSQLLoadResult:
        """Загрузить проверенный request и вернуть counts и transaction outcome.

        Реализация отвечает за scope, staging, повторные schema/grants checks,
        resource guard и rollback. Ошибки/отмена сохраняют известный commit
        outcome; coordinator не повторяет DML и не выполняет DDL.
        """
        ...


@dataclass(frozen=True, kw_only=True)
class DatabaseBinding:
    """Связать DB adapters одного target с политикой и ресурсами текущего run.

    ``inspector``/``request`` задают read-only inspection; ``policy``/``planner`` —
    scope и dry-run. ``reader`` нужен для проверок DB constraints.
    ``loader``, ``staging`` и ``references`` обязательны только для live load;
    ``semantic_catalog`` добавляет trusted aliases/rules к mapping.

    Callback references(snapshot, run_id, deadline) возвращает реальные
    StagingArtifactReference на сохранённые host artifacts. Hash не доказывает
    retention; staging не сохраняет payload вместо приложения. Factory получает
    SecuritySession: host передаёт тот же guard адаптерам, разделяет inspector/
    writer principals и закрывает clients. Создание DTO не выполняет I/O;
    несогласованные target/policy отклоняются при операции до записи.
    """

    inspector: DatabaseAdapter = field(repr=False)
    request: DatabaseInspectionRequest
    policy: DryRunPolicy
    planner: DryRunPlanner = field(repr=False)
    reader: ConstraintReader | None = field(default=None, repr=False)
    loader: Loader | None = field(default=None, repr=False)
    staging: RunStagingStore | None = field(default=None, repr=False)
    references: (
        Callable[
            [DryRunRequest, str, datetime],
            Awaitable[tuple[StagingArtifactReference, ...]],
        ]
        | None
    ) = field(default=None, repr=False)
    semantic_catalog: DatabaseSemanticCatalog | None = field(default=None, repr=False)


@dataclass(frozen=True, kw_only=True)
class SDKDependencies:
    """Явная конфигурация политик, адаптеров и фабрик SDK без глобального состояния.

    security/parsing/privacy/injection/provenance/ranking задают границы stages.
    semantic_mapping задаёт бюджеты, пороги и confidence policy LLM mapping.
    routing/providers/scanner разрешают LLM только после request-bound approval;
    отсутствие scanner не разрешает отправку. database/audit — фабрики одного
    SecuritySession; hooks получают безопасные AuditEvent последовательно.
    actor_id обязателен для audit. business_rules/json_schema задают validation.

    max_snapshot_bytes ограничивает суммарные snapshots (default 4 MiB),
    max_batches/max_records — их объём (default 1000), cleanup_seconds — время
    cleanup (default 5 s). clock/new_id внедряют UTC время и UUID без чтения
    окружения. Некорректные limits, более 16 hooks или audit без actor_id дают
    ValueError при создании; остальные admission gates действуют при операции.
    Конструктор не открывает соединения. Clients, ключи и transport закрывает host.
    """

    security: SecurityPolicy = field(default_factory=SecurityPolicy)
    parsing: ParsingPolicy = field(default_factory=ParsingPolicy)
    privacy: DetectionPolicy = field(default_factory=DetectionPolicy)
    injection: InjectionPolicy = field(default_factory=InjectionPolicy)
    provenance: ProvenancePolicy = field(default_factory=ProvenancePolicy)
    ranking: DeterministicMappingOptions = field(
        default_factory=DeterministicMappingOptions
    )
    semantic_mapping: SemanticMappingOptions = field(
        default_factory=SemanticMappingOptions
    )
    routing: LLMRoutingPolicy | None = None
    providers: tuple[LLMProvider, ...] = field(default=(), repr=False)
    scanner: SecurityScanner | None = field(default=None, repr=False)
    database: Callable[[SecuritySession], DatabaseBinding] | None = field(
        default=None, repr=False
    )
    audit: Callable[[SecuritySession], AuditChain] | None = field(
        default=None, repr=False
    )
    actor_id: UUID | None = None
    hooks: tuple[Callable[[AuditEvent], Awaitable[None]], ...] = field(
        default=(), repr=False
    )
    business_rules: BusinessRuleSet | None = field(default=None, repr=False)
    json_schema: str | None = field(default=None, repr=False)
    max_snapshot_bytes: int = 4_194_304
    max_batches: int = 1000
    max_records: int = 1000
    cleanup_seconds: float = 5
    clock: Callable[[], datetime] = utc_now
    new_id: Callable[[], UUID] = uuid4

    def __post_init__(self) -> None:
        if (
            not 1 <= self.max_snapshot_bytes <= 33_554_432
            or not 1 <= self.max_batches <= 1000
            or not 1 <= self.max_records <= 10_000
        ):
            raise ValueError("Некорректные bounded snapshot limits")
        if not 0 < self.cleanup_seconds <= 30 or len(self.hooks) > 16:
            raise ValueError("Некорректные lifecycle limits")
        if self.audit is not None and self.actor_id is None:
            raise ValueError("Signed audit требует actor_id")
