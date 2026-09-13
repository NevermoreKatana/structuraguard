"""Sync facade без фонового thread и без вложенного event loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

from .config import SDKConfig
from .contracts.common import LoadOperation
from .exceptions import OperationNotImplementedError
from .parsers import ParserRegistry

if TYPE_CHECKING:
    from .contracts.database import DatabaseCatalog
    from .contracts.mapping import MappingPlan, MappingPlanValidationResult
    from .contracts.mapping_validation import MappingPlanInputReport
    from .contracts.orchestration import IngestResult
    from .contracts.parsing import ParsePlan, ParsePlanValidationResult
    from .contracts.profiling import NormalizedDataProfile
    from .pipeline.composition import SDKDependencies
    from .pipeline.mapping import MappingProposal
    from .pipeline.source import NormalizedData, SourceAnalysis, SourceRequest
    from .structure.hybrid import HybridAnalysis


import asyncio
from collections.abc import Callable, Coroutine
from types import TracebackType
from typing import Self

from .exceptions import StructuraGuardError
from .sdk import AsyncStructuraGuard


class StructuraGuard:
    """Синхронная оболочка AsyncStructuraGuard с теми же DTO и security gates.

    ``config``, ``parser_registry`` и ``dependencies`` передаются async фасаду;
    конструктор не выполняет I/O. Параметры, результаты, ошибки стадий и побочные
    эффекты методов совпадают с одноимёнными методами AsyncStructuraGuard.
    Вызовы внутри активного event loop дают StructuraGuardError с кодом
    SYNC_API_IN_ASYNC_CONTEXT до создания coroutine; используйте async API.

    Для пошагового сценария ``with StructuraGuard(...) as sdk`` сохраняет один
    asyncio.Runner и закрывает source leases при выходе. Вне контекста каждый
    вызов использует asyncio.run; clients с привязкой к loop требуют общего
    контекста. Источники из inspect_source закрываются close_source/close.
    Владение внешними clients и transport остаётся у приложения.
    """

    def __init__(
        self,
        *,
        config: SDKConfig | None = None,
        parser_registry: ParserRegistry | None = None,
        dependencies: SDKDependencies | None = None,
    ) -> None:
        self._async_sdk = AsyncStructuraGuard(
            config=config, parser_registry=parser_registry, dependencies=dependencies
        )
        self._runner: asyncio.Runner | None = None
        self._sources: list[SourceAnalysis] = []

    @property
    def config(self) -> SDKConfig:
        """Явная immutable конфигурация async engine."""
        return self._async_sdk.config

    @property
    def parsers(self) -> ParserRegistry:
        """Вернуть реестр async фасада без копирования и автоматического поиска."""
        return self._async_sdk.parsers

    @staticmethod
    def _ensure_sync_context() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise StructuraGuardError(
            error_code="SYNC_API_IN_ASYNC_CONTEXT", message="SYNC_API_IN_ASYNC_CONTEXT"
        )

    def _call[T](self, operation: Callable[[], Coroutine[object, object, T]]) -> T:
        self._ensure_sync_context()
        if self._runner:
            return self._runner.run(operation())
        return asyncio.run(operation())

    def __enter__(self) -> Self:
        """Открыть общий Runner; повторный вход даёт SDK_RUN_BUSY, I/O ещё нет."""
        self._ensure_sync_context()
        if self._runner is not None:
            raise StructuraGuardError(error_code="SDK_RUN_BUSY", message="SDK_RUN_BUSY")
        self._runner = asyncio.Runner()
        self._runner.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close_source(self, source: SourceAnalysis) -> None:
        """Закрыть lease source, полученный inspect_source этого фасада.

        Освобождает ресурсы обработки через sync loop, не закрывает host transport.
        Чужой или уже удалённый lease даёт SDK_SOURCE_OWNER_MISMATCH;
        вызов в активном event loop отклоняется SYNC_API_IN_ASYNC_CONTEXT.
        """
        if not any(item is source for item in self._sources):
            raise StructuraGuardError(
                error_code="SDK_SOURCE_OWNER_MISMATCH",
                message="SDK_SOURCE_OWNER_MISMATCH",
            )
        self._call(source.aclose)
        self._sources = [item for item in self._sources if item is not source]

    def close(self) -> None:
        """Закрыть сохранённые source leases и Runner с ограниченным cleanup.

        Повторное закрытие допустимо; внешние adapters остаются у host. Вызов
        внутри активного event loop даёт SYNC_API_IN_ASYNC_CONTEXT.
        """
        self._ensure_sync_context()
        try:
            for source in tuple(self._sources):
                self.close_source(source)
        finally:
            if self._runner:
                self._runner.close()
                self._runner = None

    def inspect_source(self, source: SourceRequest) -> SourceAnalysis:
        """Прочитать SourceRequest и вернуть SourceAnalysis после technical parsing.

        Source I/O и PipelineError — как у AsyncStructuraGuard.inspect_source;
        семантика, LLM и БД не вызываются. Lease сохраняется до close_source/close.
        В активном loop возникает SYNC_API_IN_ASYNC_CONTEXT.
        """
        result = self._call(lambda: self._async_sdk.inspect_source(source))
        self._sources.append(result)
        return result

    def analyze_structure(
        self, source: SourceAnalysis, *, saved_plan: ParsePlan | None = None
    ) -> HybridAnalysis:
        """Вернуть HybridAnalysis для source, проверив необязательный saved_plan.

        Кеш, LLM I/O и PipelineError — как у AsyncStructuraGuard.analyze_structure;
        результат не разрешает DB load. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.analyze_structure(source, saved_plan=saved_plan)
        )

    def create_parse_plan(
        self, source: SourceAnalysis, *, structure: HybridAnalysis | None = None
    ) -> ParsePlan | None:
        """Вернуть draft ParsePlan либо None для source и необязательного structure.

        Анализ, возможный LLM I/O и ошибки — как у async create_parse_plan;
        draft требует validation. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.create_parse_plan(source, structure=structure)
        )

    def validate_parse_plan(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> ParsePlanValidationResult:
        """Проверить plan против source и вернуть ParsePlanValidationResult.

        Replay и PipelineError — как у async validate_parse_plan; генерации плана
        и DB I/O нет. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.validate_parse_plan(source, plan=plan)
        )

    def parse_semantically(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> NormalizedData:
        """Вернуть NormalizedData после validation и полного исполнения plan.

        Source lease, лимиты и PipelineError — как у async parse_semantically;
        DB operations нет. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(lambda: self._async_sdk.parse_semantically(source, plan=plan))

    def profile_records(self, source: NormalizedData) -> NormalizedDataProfile:
        """Вернуть NormalizedDataProfile для подтверждённого source snapshot.

        Binding, кеш и ошибки — как у async profile_records; LLM/DB I/O нет.
        Профиль чувствителен. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(lambda: self._async_sdk.profile_records(source))

    def inspect_database(
        self, *, source: NormalizedData | None = None
    ) -> DatabaseCatalog:
        """Вернуть свежий DatabaseCatalog, необязательно связанный с source.

        Read-only DB I/O, scope и ошибки — как у async inspect_database;
        запись запрещена. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(lambda: self._async_sdk.inspect_database(source=source))

    def create_mapping_plan(
        self,
        source: NormalizedData,
        *,
        database: DatabaseCatalog | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
    ) -> MappingProposal:
        """Вернуть MappingProposal для source, database и выбранной operation.

        Inspection, LLM policy и ошибки — как у async create_mapping_plan;
        proposal не разрешает запись. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.create_mapping_plan(
                source, database=database, operation=operation
            )
        )

    def validate_mapping_plan(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        database: DatabaseCatalog | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        """Вернуть validation или input report для plan, source и database.

        Scope, read-only I/O и ошибки — как у async validate_mapping_plan;
        target не меняется. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.validate_mapping_plan(
                source, plan=plan, database=database
            )
        )

    def execute(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        """Вернуть IngestResult после проверки source/plan и dry-run либо load.

        dry_run исключает writer; idempotency_key передаётся ledger. Возможные
        writes, ошибки и commit outcomes — как у async execute, lease остаётся
        у caller до close_source/close. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.execute(
                source, plan=plan, dry_run=dry_run, idempotency_key=idempotency_key
            )
        )

    def ingest(
        self,
        source: SourceRequest,
        *,
        dry_run: bool = False,
        parse_plan: ParsePlan | None = None,
        mapping_plan: MappingPlan | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        """Пройти SourceRequest→IngestResult и закрыть созданный source lease.

        Saved plans, operation, dry_run, idempotency_key, внешние I/O и ошибки
        имеют семантику async ingest; security gates и отсутствие DML retry
        сохраняются. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(
            lambda: self._async_sdk.ingest(
                source,
                dry_run=dry_run,
                parse_plan=parse_plan,
                mapping_plan=mapping_plan,
                operation=operation,
                idempotency_key=idempotency_key,
            )
        )

    def analyze(self, source: SourceRequest) -> IngestResult:
        """Вернуть IngestResult анализа source до предложения MappingPlan.

        Source/LLM/read-only DB I/O и ошибки — как у async analyze, staging/load
        отсутствуют; lease закрывается. Активный loop: SYNC_API_IN_ASYNC_CONTEXT.
        """
        return self._call(lambda: self._async_sdk.analyze(source))

    create_plan = create_mapping_plan
    validate_plan = validate_mapping_plan

    def propose_schema(self, *args: object, **kwargs: object) -> Never:
        """Отклонить args/kwargs без I/O кодом SDK_OPERATION_NOT_IMPLEMENTED.

        Поднимает OperationNotImplementedError; внутри активного loop первым
        возникает StructuraGuardError с кодом SYNC_API_IN_ASYNC_CONTEXT.
        """
        self._ensure_sync_context()
        raise OperationNotImplementedError("propose_schema")
