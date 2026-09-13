# SDK orchestrator M15

`AsyncStructuraGuard` соединяет существующие parsers, M6 analysis, M8–M12
validation и M13 load. Вход — `SourceRequest` с принадлежащим приложению
`SourceStream`; транспорт не открывается фасадом. Минимальный пример без сети:

```python
import asyncio

from structuraguard import AsyncStructuraGuard
from structuraguard.contracts import SemanticParsingMode
from structuraguard.contracts.profiling import NormalizedDataProfile
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SDKDependencies, SourceRequest


class BytesStream:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.value[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


async def inspect_records() -> NormalizedDataProfile | None:
    registry = ParserRegistry()
    registry.register(DelimitedTextParser())
    sdk = AsyncStructuraGuard(
        parser_registry=registry,
        dependencies=SDKDependencies(
            security=SecurityPolicy(allowed_formats=("csv",), parser_trust="trusted"),
            parsing=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
        ),
    )
    source = await sdk.inspect_source(SourceRequest(
        stream=BytesStream(b"name,n\nAda,1\nBob,2\n"), display_name="sample.csv",
    ))
    async with source:
        plan = await sdk.create_parse_plan(source)
        if plan is None:
            return None  # Причины — в (await sdk.analyze_structure(source)).issues.
        normalized = await sdk.parse_semantically(source, plan=plan)
        return await sdk.profile_records(normalized)


if __name__ == "__main__":
    profile = asyncio.run(inspect_records())
    if profile is None:
        raise SystemExit("Источник требует review")
    print(f"records={profile.record_count}")
```

Сохраните блок в `.py` и запустите в окружении с установленным SDK: ожидается
`records=2`. Пример заканчивается профилем, поэтому DB/LLM adapters не нужны.
В приложении с уже работающим event loop вызывайте `await inspect_records()`.

Конструктор не выполняет I/O. `SDKConfig` и `parser_registry` сохраняют прежнее
назначение и identity; `dependencies` — новая явная composition. Пустой registry
не обнаруживает adapters автоматически. Default security policy запрещает parsing.

Канонические требования: [M15][spec-m15], [режимы работы][spec-modes],
[pipeline][spec-pipeline], [публичный API][spec-api], [результат][spec-result],
[статусы][spec-statuses] и [безопасность][spec-security]. Эта страница описывает
подтверждённое поведение реализации; наличие сценария в ТЗ не означает его готовность.

## Публичные операции {#operations}

Async-методы ожидаются через `await`; sync facade предоставляет те же параметры
и типы результата. `SourceAnalysis` и `NormalizedData` — frozen runtime DTO,
не сериализуемые полномочия. Их snapshots принадлежат одному SDK и source lease.

| Метод | Вход и результат | Граница |
|---|---|---|
| `inspect_source(source)` | `SourceRequest → SourceAnalysis` | Gate, detection, отдельный technical parsing, подтверждённый physical manifest |
| `analyze_structure(source, saved_plan=None)` | `SourceAnalysis → HybridAnalysis` | Отдельный structural profile, M6 policy и cached analysis |
| `create_parse_plan(source, structure=None)` | `SourceAnalysis → ParsePlan \| None` | Использует analysis; issues/draft не означают принятие |
| `validate_parse_plan(source, plan=...)` | `SourceAnalysis → ParsePlanValidationResult` | Полный physical replay; не вызывает LLM |
| `parse_semantically(source, plan=...)` | `SourceAnalysis → NormalizedData` | Повторная validation до execution; preview без EOF отвергается |
| `profile_records(source)` | `NormalizedData → NormalizedDataProfile` | Проверяет binding; cache только для этого normalized snapshot |
| `inspect_database(source=None)` | optional `NormalizedData → DatabaseCatalog` | Fresh read-only inspection; возможен самостоятельный вызов |
| `create_mapping_plan(source, database=None, operation=INSERT_ONLY)` | `NormalizedData → MappingProposal` | M9, при необходимости M10; явная полная assembly выбранных candidates |
| `validate_mapping_plan(source, plan=..., database=None)` | `NormalizedData → MappingPlanValidationResult \| MappingPlanInputReport` | M11 проверяет plan, catalog, profile и trusted scope до target operations |
| `execute(source, plan=..., dry_run=False, idempotency_key=None)` | `NormalizedData → IngestResult` | M11, M12, pre-load security, staging и M13 |
| `ingest(source, dry_run=False, parse_plan=None, mapping_plan=None, operation=INSERT_ONLY, idempotency_key=None)` | `SourceRequest → IngestResult` | Полный pipeline и закрытие собственного source lease |
| `analyze(source)` | `SourceRequest → IngestResult` | До proposal MappingPlan, без staging/load |

`create_plan` и `validate_plan` — aliases mapping-методов. `propose_schema`
сохраняет явный `SDK_OPERATION_NOT_IMPLEMENTED`: DDL не входит в pipeline.
Пошаговая ошибка даёт `PipelineError`, наследник `StructuraGuardError`, с полным
`.result` и безопасным `error_code`. `ingest`, `execute` и `analyze` возвращают
частичный `IngestResult` для processing failures/review. Некорректная constructor
configuration и использование чужого/закрытого lease — typed exception.
`CancelledError` распространяется отдельно, не превращается в успешный result.
При подтверждённом commit отмена/ошибка cleanup сохраняет факт записи и warnings.
`execute` завершает run, но его source lease закрывает caller; `ingest` и `analyze`
закрывают созданный ими lease сами. Ни один из них не закрывает host transport.

| Ошибка / код | Что получает caller |
|---|---|
| `PipelineError` из `structuraguard.pipeline.session` | Пошаговый отказ: `.error_code` и чувствительный `.result`; для logs — `.result.safe_summary()` |
| `SDK_SOURCE_OWNER_MISMATCH`, `SOURCE_SNAPSHOT_EXPIRED` | `StructuraGuardError`: DTO принадлежит другому SDK либо lease закрыт |
| `SDK_RUN_BUSY`, `SDK_INVALID_TRANSITION` | `StructuraGuardError`: параллельная операция одного lease либо недопустимый переход |
| `SDK_DATABASE_DEPENDENCY_REQUIRED` | Нет database factory; пошаговый inspection даёт PipelineError, конечная операция — частичный result |
| `PROCESSING_TIMEOUT`, `SECURITY_LIMIT_EXCEEDED` | Обработка остановлена, stage/code сохранены; новая попытка DML автоматически не выполняется |
| `SYNC_API_IN_ASYNC_CONTEXT` | `StructuraGuardError` до создания coroutine; нужен async facade |

Текст внешнего exception не переносится в безопасную диагностику. Для расследования
используйте stage и `cause_codes`; полного исходного traceback в результате нет.

## Composition и владение

`SDKDependencies` принимает policies, clock, UUID factory, event hooks,
base `SecurityScanner`, tuple providers, optional business rules/JSON Schema,
фабрики database и audit. Общий `SecuritySession` создаётся для каждого run;
фабрики получают именно его. Глобальных registries/clients нет.

`DatabaseBinding` соединяет read-only `DatabaseAdapter`, `DatabaseInspectionRequest`,
`DryRunPolicy`, `DryRunPlanner`, optional `ConstraintReader`, отдельный `Loader`,
`RunStagingStore`, callback `references` и semantic catalog. Production composition
передаёт один target/policy и `resources` в существующие
`PostgreSQLDatabaseAdapter`, `DatabaseConstraintReader`, `PostgreSQLDryRunPlanner`,
`PostgreSQLLoader`. Inspector и writer используют разные principals. Scope
проверяется до inspection, план — до key reads, DML — внутри M13 transaction.
См. [loader](loader.md) и [dry-run](dry_run.md).

Callback `references(snapshot, run_id, deadline)` сохраняет реальные artifacts
у host и возвращает `StagingArtifactReference`. Staging хранит references и
metadata; наличие hashes не доказывает retention. Bootstrap staging/ledger/audit
делает администратор отдельно. SDK не создаёт таблицы. Host управляет lifecycle
providers, transports, artifact store, ключами и доставкой audit.

Source, physical и normalized snapshots ограничены суммарным byte budget
(default 4 MiB), числом batches (1000) и records (1000), а также более строгими
component policies. Byte cap сужает snapshot intake до накопления. Превышение
лимита — отказ; spill/durable replay и URL/path transport не добавлены.
Для risky formats в strict mode и недоверенных parsers возвращается
`SECURITY_SANDBOX_REQUIRED`: готового OS sandbox backend здесь нет.

## States, hooks и ошибки

Последовательность проверяется в `pipeline/state.py`:

```text
CREATED → SOURCE_PROBING → TECHNICAL_PARSING → STRUCTURE_PROFILING
→ STRUCTURE_ANALYZING → PARSE_PLAN_CREATED → PARSE_PLAN_VALIDATING
→ SEMANTIC_PARSING → NORMALIZED_DATA_PROFILING → DATABASE_INSPECTING
→ MAPPING → MAPPING_PLAN_CREATED → MAPPING_PLAN_VALIDATING
→ NORMALIZING → VALIDATING → STAGING → LOADING → COMPLETED
```

Standalone inspection входит из `CREATED` в `DATABASE_INSPECTING`. Saved mapping
пропускает `MAPPING`, но сохраняет `MAPPING_PLAN_CREATED` и validation.
Повторные validation допускаются до execution; другие повторные stage effects
отвергаются. Одновременные операции одного lease дают `SDK_RUN_BUSY`.
После terminal outcome любая новая операция этого source, включая повторный
`execute`, даёт `SDK_INVALID_TRANSITION` до изменения сохранённого результата.
`execute` резервирует выполнение до изменения `dry_run` и удерживает защиту
до завершения terminal hooks. Параллельный или повторный вызов из hook получает
`SDK_RUN_BUSY`, не меняя режим и отчёты активного execute.

Неоднозначность, неподдержанная семантика и несовпавшие fingerprints дают
`NEEDS_REVIEW`; security deny — `REJECTED_SECURITY`; обычная ошибка — `FAILED`;
отмена — `CANCELLED`. Подтверждённый rollback отмечается `ROLLED_BACK` и отдельным
`TransactionOutcome.ROLLED_BACK`. Неизвестный commit сохраняет `UNKNOWN`; повторного
DML фасад не делает. После commit failures observers/cleanup дают
`COMPLETED_WITH_WARNINGS`, сохраняя counts и commit evidence.

Hooks вызываются последовательно в loop вызывающего приложения для `run_started`,
`stage_started`, `stage_completed`, `run_finished`. Они получают `AuditEvent`
с opaque IDs, UTC временем и fingerprints. Raw values, DSN, prompts и тексты
исключений туда не передаются. Ошибка hook до загрузки останавливает pipeline;
после commit не меняет факт записи. Локальный terminal security report описывает
выполненные policy gates и не используется как approval внешнего LLM.

SDK не логирует внешние exceptions: сохраняет исходный typed error code,
исходную стадию и безопасный тип стандартной ошибки в `cause_codes`.
Raw exception chain/notes не публикуются. Adapter errors, уже очищенные
самим adapter, не восстанавливаются. `safe_summary()` результата подходит
для диагностики; полный JSON reports/plans содержит чувствительные данные.

При заданной audit factory проверяется ключ до первого source stage. HMAC chain
сохраняет start, parser, validation и terminal events; result содержит anchors.
Атомарный load audit и durable ledger принадлежат M13 и подключаются его policy.
Пустые `audit_references` означают отсутствие настроенного signed store.
Обычные hooks не подтверждают durable delivery. Отказ audit отражается `AUDIT_GAP`.

## Policies, cancellation и retry

- `deterministic` запрещает model calls в parsing и mapping. `llm_assisted`
  использует детерминированный план для распознаваемых таблиц; `llm_first`
  сохраняет семантику M6. Повторные строки исполняют один ParsePlan.
- `LLMRoutingMode.NO_LLM` запрещает обе LLM-стадии. В сочетании с `llm_first`
  получается review, а не скрытая смена parsing mode.
- До egress выполняются content classification, injection detection и scanner
  для точного request. Один scan анализирует bounded физический текст целиком,
  включая nested values и metadata. Отсутствие scanner не разрешает LLM.
- Credential hints JSON/XML, табличных колонок и metadata дополнительно проходят
  M14 `classify_fields`, включая `custom_secret_fields`. Они только повышают
  classification; nested credential values сохраняют ограничение родителя.
  Тот же ограниченный scan повторяется перед staging/load. Дополнительные
  privacy summaries не подменяют request-bound scanner approval.
- M6 использует один явно выбранный provider/route; неподдержанная комбинация
  нескольких routes отклоняется при admission. M10 использует существующий router
  и только его явные retry/fallback policies. Нового orchestration retry нет.
- Один monotonic processing deadline проходит через этапы. Parser имеет отдельный
  меньший deadline; общий ресурсный бюджет LLM сужается routing budget и разделяется
  между parsing/mapping. Retry не возвращает уже потраченный бюджет.
- Вокруг transactional `loader.execute` нет внешнего `asyncio.timeout`: adapter
  получает `resources` напрямую и сохраняет корректный COMMIT outcome. Read-only
  planner ограничивается deadline. DB errors не запускают повторный DML.
- Cancellation закрывает semantic iterator; cleanup получает отдельный конечный
  reserve. Незагруженный OPEN/SEALED staging переводится в FAILED/CANCELLED.
  EXECUTING/UNKNOWN не объявляются откатанными без evidence от loader.

## Результат, reuse и dry-run

`IngestResult` экспортируется из `structuraguard` и `structuraguard.contracts`.
Он содержит source/extraction/parse/normalized/database/mapping fingerprints,
ParsePlan/MappingPlan, provider attempts, profile, validation/load/security reports,
audit events и signed references. Отчёт отсутствует, если stage не выполнялся.
Если loader потерял ответ после записи, но staging уже подтверждает `COMMITTED`,
SDK сохраняет `TransactionOutcome.COMMITTED` и `COMPLETED_WITH_WARNINGS`.
`load_result`/`load_report` остаются `None`: staging не позволяет восстановить
фактические counts. Код ошибки остаётся в warnings; отмена после такого commit
даёт `LOAD_CANCELLED_AFTER_COMMIT`. `NOT_STARTED` в этом случае не возвращается.
При десериализации envelope проверяет также database/normalized/mapping bindings
вложенных `PostgreSQLLoadResult` и `DryRunExecutionPlan`.
`dry_run=True` не создаёт staging run и не вызывает loader; результат содержит
`DryRunExecutionPlan`, `TransactionOutcome.DRY_RUN`, `loaded_records=0` и отдельный
`would_load_records`. Target, sequences и persistent staging не меняются.

ParsePlan повторно проверяется на том же source/extraction/profile. MappingPlan
повторно проверяется на **точном** normalized fingerprint и fresh catalog/policy.
M5 record IDs включают run identity, поэтому новый run на тех же bytes может иметь
другой normalized fingerprint. SDK не переписывает saved MappingPlan молча:
`MAPPING_SOURCE_LINEAGE_MISMATCH` требует нового плана. Повторное применение
MappingPlan на том же живом snapshot поддержано. Смена schema/grants перед записью
проверяется повторно M13. Durable idempotency подключается через loader ledger;
сам фасад не строит cache и не обеспечивает cross-run resume.

M13 принимает copy-only normalized values. Если M12 policy требует изменения
значений, SDK возвращает `DRY_RUN_PROVENANCE_UNVERIFIED`, сохраняя отсутствие
записи. Числовые lexemes некоторых technical parsers остаются строками; произвольное
приведение к numeric DB columns не добавлено. Generated PK propagation,
автоматическое разрешение неизвестных CHECK и unsafe SQL отсутствуют.

Sync-вызов внутри активного event loop даёт `SYNC_API_IN_ASYNC_CONTEXT` до создания
coroutine. `with StructuraGuard(...) as sdk:` сохраняет один `asyncio.Runner` между
этапами и закрывает принадлежащие facade leases при выходе. Вне context manager
finite calls используют `asyncio.run`; `close_source(source)`/`close()` закрывают
пошаговые leases. Host providers с привязкой к loop должны жить в том же loop.

## Синхронный пример

Этот блок запускается отдельно и также печатает `records=2`. Контекст сохраняет
один loop для всех этапов и закрывает source lease даже при ошибке.

```python
from structuraguard import StructuraGuard
from structuraguard.contracts import SemanticParsingMode
from structuraguard.contracts.security import SecurityPolicy
from structuraguard.parsers import ParserRegistry
from structuraguard.parsers.builtin import DelimitedTextParser
from structuraguard.parsing import ParsingPolicy
from structuraguard.pipeline import SDKDependencies, SourceRequest


class BytesStream:
    def __init__(self, value: bytes) -> None:
        self.value = value
        self.offset = 0

    async def read(self, size: int) -> bytes:
        chunk = self.value[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


registry = ParserRegistry()
registry.register(DelimitedTextParser())
with StructuraGuard(
    parser_registry=registry,
    dependencies=SDKDependencies(
        security=SecurityPolicy(allowed_formats=("csv",), parser_trust="trusted"),
        parsing=ParsingPolicy(mode=SemanticParsingMode.DETERMINISTIC),
    ),
) as sdk:
    source = sdk.inspect_source(SourceRequest(
        stream=BytesStream(b"name,n\nAda,1\nBob,2\n"), display_name="sample.csv",
    ))
    plan = sdk.create_parse_plan(source)
    if plan is None:
        raise SystemExit("Источник требует review")
    normalized = sdk.parse_semantically(source, plan=plan)
    profile = sdk.profile_records(normalized)
    print(f"records={profile.record_count}")
```

## Совместимость и подтверждение

Миграция с M1: stubs принимают строгие DTO вместо произвольных `*args/**kwargs`;
constructor arguments сохранены. Полный список решений —
[ADR 0034](adr/0034-sdk-orchestrator.md), исходный [план M15](plans/M15_sdk_orchestrator.md).

Оба блока извлекаются из этой страницы и исполняются как самостоятельные scripts
в `tests/docs/test_m15_example.py`; тест запрещает сетевые соединения и проверяет
число записей. PostgreSQL source→report подтверждён отдельным integration suite,
а не этим offline-примером. Трассировка требований и ограничений:
[приёмка M15](plans/M15_acceptance.md), [security review](plans/M15_security_review.md).

[spec-m15]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m15-sdk-orchestrator
[spec-modes]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#6-режимы-работы-sdk
[spec-pipeline]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#7-полный-pipeline
[spec-api]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#23-публичный-api-sdk
[spec-result]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#24-результат-работы
[spec-statuses]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#25-статусы-pipeline
[spec-security]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#20-информационная-безопасность
