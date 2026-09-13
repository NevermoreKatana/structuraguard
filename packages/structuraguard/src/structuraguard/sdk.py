"""Асинхронный typed facade; dependencies подключаются явно и без import-time I/O."""

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
    from .pipeline.orchestrator import Orchestrator
    from .pipeline.source import NormalizedData, SourceAnalysis, SourceRequest
    from .structure.hybrid import HybridAnalysis


class AsyncStructuraGuard:
    """Асинхронный SDK с явными зависимостями и проверяемыми этапами импорта.

    Параметры конструктора: ``config`` сохраняет конфигурацию SDK;
    ``parser_registry`` задаёт реестр этого экземпляра; ``dependencies`` задаёт
    политики, адаптеры и фабрики ресурсов каждого запуска. Пустой реестр
    не обнаруживает плагины; политика по умолчанию запрещает parsing.
    Конструктор не выполняет I/O и не читает окружение.

    Пошаговые методы требуют собственный открытый SourceAnalysis. Чужой,
    закрытый, занятый или завершённый запуск отклоняется StructuraGuardError.
    Ошибка обработки даёт PipelineError с частичным ``result``; конечные
    операции возвращают IngestResult. Отмена сохраняет CancelledError,
    кроме отмены после подтверждённого commit: факт записи остаётся в результате.
    Deadline и лимиты действуют на весь запуск, повторов DML фасад не делает.

    Приложение закрывает свои DB/LLM clients и SourceStream. Lease пошагового
    источника нужно закрыть через ``async with source`` или ``source.aclose()``.
    Полные DTO могут содержать PII; для журналирования результата предназначен
    ``IngestResult.safe_summary()``.
    """

    def __init__(
        self,
        *,
        config: SDKConfig | None = None,
        parser_registry: ParserRegistry | None = None,
        dependencies: SDKDependencies | None = None,
    ) -> None:
        self._config = config if config is not None else SDKConfig()
        self._parsers = (
            parser_registry if parser_registry is not None else ParserRegistry()
        )
        self._dependencies = dependencies
        self._orchestrator: Orchestrator | None = None

    @property
    def config(self) -> SDKConfig:
        """Вернуть ту же явную immutable конфигурацию."""
        return self._config

    @property
    def parsers(self) -> ParserRegistry:
        """Вернуть реестр этого экземпляра без копирования и автопоиска plugins."""
        return self._parsers

    def _engine(self) -> Orchestrator:
        from .pipeline.composition import SDKDependencies
        from .pipeline.orchestrator import Orchestrator

        if self._orchestrator is None:
            self._orchestrator = Orchestrator(
                self._dependencies or SDKDependencies(), self._parsers
            )
        return self._orchestrator

    async def inspect_source(self, source: SourceRequest) -> SourceAnalysis:
        """Прочитать SourceRequest и вернуть физический snapshot SourceAnalysis.

        Читает ``source.stream`` в пределах лимитов, применяет security gate,
        определяет формат и вызывает technical parser. Путь/URL из metadata
        не открывает; семантический разбор, LLM и БД здесь не вызываются.
        Отказ gate/parser или timeout даёт PipelineError; отмена распространяется.
        При успехе caller закрывает lease, но сохраняет владение transport.
        """
        return await self._engine().inspect_source(source)

    async def analyze_structure(
        self, source: SourceAnalysis, *, saved_plan: ParsePlan | None = None
    ) -> HybridAnalysis:
        """Вернуть HybridAnalysis с профилем, draft и причинами review источника.

        ``source`` — открытый физический snapshot; ``saved_plan`` проверяется
        против него через replay. При отсутствии сохранённого плана parsing/LLM
        policy может разрешить внешний вызов после классификации и scanner gate.
        Повторный анализ этого lease использует кеш; другой saved_plan даёт
        PipelineError. Ошибки обработки/deadline также дают PipelineError.
        Результат анализа не разрешает загрузку в БД.
        """
        return await self._engine().analyze_structure(source, saved_plan=saved_plan)

    async def create_parse_plan(
        self, source: SourceAnalysis, *, structure: HybridAnalysis | None = None
    ) -> ParsePlan | None:
        """Получить draft ParsePlan для source либо None, если анализ не дал плана.

        ``structure`` должен совпадать с анализом этого lease, иначе возникает
        PipelineError. Если анализа ещё нет, выполняется analyze_structure
        с его политиками и возможным LLM I/O. Причины отсутствия плана доступны
        в HybridAnalysis. Draft не заменяет validation перед semantic execution;
        БД не вызывается. Отказы обработки/deadline дают PipelineError.
        """
        return await self._engine().create_parse_plan(source, structure=structure)

    async def validate_parse_plan(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> ParsePlanValidationResult:
        """Проверить недоверенный plan против полного физического source snapshot.

        Возвращает ParsePlanValidationResult с decision/issues и необязательным
        validated_plan. При первом вызове анализ использует этот saved plan;
        новый план через LLM не генерируется. БД не вызывается. Невалидный план
        описывается в отчёте; отказ стадии/deadline даёт PipelineError.
        Чужой или закрытый lease отклоняется StructuraGuardError.
        """
        return await self._engine().validate_parse_plan(source, plan=plan)

    async def parse_semantically(
        self, source: SourceAnalysis, *, plan: ParsePlan
    ) -> NormalizedData:
        """Применить plan к source после validation и вернуть NormalizedData.

        Повторно проверяет ParsePlan и читает semantic iterator до terminal batch
        в пределах общего бюджета. Результат содержит batches, manifest, report
        и связь с исходным lease; это отдельный этап после technical parsing.
        Preview, неоднозначность, отказ validation или deadline дают PipelineError.
        Отмена распространяется; этот метод не выполняет DB operations.
        """
        return await self._engine().parse_semantically(source, plan=plan)

    async def profile_records(self, source: NormalizedData) -> NormalizedDataProfile:
        """Вернуть NormalizedDataProfile для подтверждённого source snapshot.

        Проверяет lineage NormalizedData; кеш действует только для этого lease
        и точного normalized fingerprint. LLM и БД не вызываются. Несовпадение
        binding или отказ профилирования/deadline даёт PipelineError;
        чужой/закрытый lease — StructuraGuardError. Полный профиль чувствителен.
        """
        return await self._engine().profile_records(source)

    async def inspect_database(
        self, *, source: NormalizedData | None = None
    ) -> DatabaseCatalog:
        """Прочитать свежий DatabaseCatalog через внедрённый DB inspector.

        ``source`` связывает inspection с нормализованным snapshot и сначала
        обеспечивает его профиль; без source создаётся отдельный конечный run.
        Выполняет read-only DB I/O в разрешённом scope без DDL/DML.
        Отсутствующая database factory, неверный target, ошибка adapter или
        deadline дают PipelineError. Отмена распространяется; clients закрывает host.
        """
        return await self._engine().inspect_database(source=source)

    async def create_mapping_plan(
        self,
        source: NormalizedData,
        *,
        database: DatabaseCatalog | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
    ) -> MappingProposal:
        """Вернуть MappingProposal для нормализованного source и целевого каталога.

        Обеспечивает профиль и inspection; ``database``, если задан, должен
        совпадать с каталогом run. ``operation`` задаёт операцию нового плана.
        Детерминированный выбор дополняется LLM только по явной policy после
        scanner gate; записи в БД нет. Неполное/неоднозначное покрытие даёт
        proposal без plan, а отказы gate/adapter/deadline — PipelineError.
        """
        return await self._engine().create_mapping_plan(
            source, database=database, operation=operation
        )

    async def validate_mapping_plan(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        database: DatabaseCatalog | None = None,
    ) -> MappingPlanValidationResult | MappingPlanInputReport:
        """Проверить plan по точному source fingerprint, профилю и DB scope.

        ``database`` должен совпадать с каталогом run; при его отсутствии
        используется уже полученный каталог либо выполняется read-only inspection.
        Возвращает MappingPlanValidationResult или MappingPlanInputReport при
        отклонённом входе, не вызывает LLM и не пишет в target. Отказ стадии
        или deadline даёт PipelineError. Успешный отчёт не отменяет повторные
        schema/grants checks загрузчика перед DML и COMMIT.
        """
        return await self._engine().validate_mapping_plan(
            source, plan=plan, database=database
        )

    async def execute(
        self,
        source: NormalizedData,
        *,
        plan: MappingPlan,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        """Проверить source/plan и вернуть IngestResult после dry-run либо загрузки.

        ``source`` — живой NormalizedData; ``plan`` повторно проходит M11 до
        проверки records и pre-load security gate. ``dry_run=True`` выполняет
        read-only прогноз без staging writer/loader. При False нужны внедрённые
        loader, staging и references; возможна транзакционная запись в target.
        ``idempotency_key`` передаётся ledger policy; автоматического DML retry нет.

        Ошибки обработки/review сохраняются в частичном результате. Неверный lease
        даёт StructuraGuardError; CancelledError до commit распространяется.
        UNKNOWN не означает rollback. Метод завершает run, но lease закрывает caller.
        """
        return await self._engine().execute(
            source, plan=plan, dry_run=dry_run, idempotency_key=idempotency_key
        )

    async def ingest(
        self,
        source: SourceRequest,
        *,
        dry_run: bool = False,
        parse_plan: ParsePlan | None = None,
        mapping_plan: MappingPlan | None = None,
        operation: LoadOperation = LoadOperation.INSERT_ONLY,
        idempotency_key: str | None = None,
    ) -> IngestResult:
        """Выполнить source→report и вернуть полный либо частичный IngestResult.

        Читает SourceRequest; ``parse_plan``/``mapping_plan`` используются только
        после проверки fingerprints. ``operation`` влияет на создаваемый mapping;
        у сохранённого плана действует его собственная операция.
        ``dry_run=True`` исключает staging writer и target writes; False разрешает
        загрузку через явно внедрённые adapters после всех gates.
        ``idempotency_key`` относится к loader ledger, а не к кешу ingest.

        Возможны source/LLM/DB I/O согласно policy. Ошибки обработки и review
        возвращаются в результате; ошибки конфигурации/admission могут возникнуть
        как исключения. CancelledError до commit распространяется. Собственный
        lease закрывается, transport/clients остаются у host; DML не повторяется.
        """
        return await self._engine().ingest(
            source,
            dry_run=dry_run,
            parse_plan=parse_plan,
            mapping_plan=mapping_plan,
            operation=operation,
            idempotency_key=idempotency_key,
        )

    async def analyze(self, source: SourceRequest) -> IngestResult:
        """Вернуть IngestResult анализа SourceRequest до предложения MappingPlan.

        Выполняет source/semantic analysis, профиль, read-only inspection и mapping
        по тем же policy, что ingest; LLM возможен только после security gates.
        Staging, record validation и load здесь не выполняются. Processing failures
        и review остаются в результате; ошибки admission и CancelledError могут
        распространяться. Собственный lease закрывается; transport закрывает host.
        """
        return await self._engine().analyze(source)

    create_plan = create_mapping_plan
    validate_plan = validate_mapping_plan

    async def propose_schema(self, *args: object, **kwargs: object) -> Never:
        """Отклонить любые args/kwargs без I/O и генерации схемы.

        Всегда поднимает OperationNotImplementedError с кодом
        SDK_OPERATION_NOT_IMPLEMENTED; DDL не входит в ingest pipeline.
        """
        raise OperationNotImplementedError("propose_schema")
