# Архитектура StructuraGuard

Статус: нормативный baseline milestone M0; package layout/scaffold уточнены в
M1, двухэтапные DTO и ports — в M2, а registry technical parsers — в M3.
Целевые format adapters и orchestrator не считаются реализованными без
milestone evidence.

Термины `MUST`, `SHOULD` и `MAY` означают соответственно обязательное требование,
рекомендацию с документируемым отклонением и допустимый вариант.

Границы MVP и критерии приёмки принадлежат
[требованиям](requirements.md), security controls —
[модели угроз](threat-model.md), а наблюдаемое поведение и имена операций —
[публичному API](public-api.md).

## Граница продукта

StructuraGuard — встраиваемая Python-библиотека, а не web-service. Библиотека
MUST оставаться применимой из CLI, ETL, notebook, фоновой задачи или приложения
на любом web framework. FastAPI, Django, Flask, Celery, Redis, UI, worker и
HTTP endpoints не входят в ядро. Демонстрационные приложения располагаются вне
SDK и зависят от него, но SDK не зависит от них.

Вызывающее приложение отвечает за hosting, process lifecycle, конфигурацию,
секреты и выбор конкретных adapters. SDK отвечает за типизированную оркестрацию,
проверку plan, соблюдение policy и безопасное завершение каждого run.

## Архитектурный стиль

Целевая архитектура использует ports and adapters. Логические границы не задают
преждевременно точную структуру Python-пакетов; она фиксируется в M1.

```text
caller / framework integration
              |
              v
async public API <--- sync facade
              |
              v
application pipeline / run coordinator
              |
              v
domain + contracts + ports
              ^
              |
parser / database / LLM / validation / security / store adapters
```

Реализованные physical adapters M4, их optional extras и ограничения описаны в
[публичном API](public-api.md#m4-technical-parsers). Долгоживущие уточнения
physical model, markup/document isolation и Tika egress закреплены в
[ADR 0004](adr/0004-lossless-physical-extraction.md),
[ADR 0005](adr/0005-safe-markup-extraction.md),
[ADR 0006](adr/0006-bounded-document-adapters.md) и
[ADR 0007](adr/0007-opt-in-tika-egress.md). Эти реализации не означают готовность
описанного ниже полного application pipeline.

### Package layout M3

Корень репозитория является виртуальным `uv` workspace и не создаёт второй
distribution. Устанавливаемый package имеет единственный source of truth:

```text
packages/structuraguard/
├── pyproject.toml
├── src/structuraguard/
│   ├── __init__.py
│   ├── config.py
│   ├── contracts/
│   ├── domain/
│   ├── exceptions.py
│   ├── parsers/
│   ├── ports/
│   ├── sdk.py
│   ├── sync_sdk.py
│   └── py.typed
└── tests/
```

Root `pyproject.toml` владеет workspace, developer tools, docs groups и явным
PEP 517 guard, отклоняющим попытку собрать корень как второй distribution;
package metadata, runtime dependency и optional extras принадлежат
`packages/structuraguard/pyproject.toml`. Core scaffold не импортирует optional
dependencies. Новые domain, ports и adapters добавляются внутрь этого package,
а не как параллельный Python-пакет.

### Слои

- **Public API** принимает типизированные inputs, создаёт run и возвращает
  типизированные results. Convenience-операции не содержат альтернативной
  бизнес-логики.
- **Application pipeline** координирует стадии, state transitions, timeout,
  cancellation, cleanup и транзакционную границу.
- **Contracts** содержат immutable physical/normalized DTO, `ParsePlan`, каталог
  БД, `MappingPlan`, validation evidence, reports и policy decisions.
- **Domain** содержит только canonical serialization, fingerprinting и чистые
  проверки lineage.
- **Ports** описывают требуемые возможности без привязки к provider или
  infrastructure.
- **Adapters** реализуют parser, DB inspection/load, LLM, staging, audit и другие
  внешние взаимодействия.

### Направление зависимостей

- `domain` MUST NOT импортировать infrastructure или framework integrations.
- Pipeline MAY зависеть от domain, contracts и ports, но MUST NOT зависеть от
  конкретного adapter.
- Adapter зависит от соответствующего port и contracts; обратная зависимость
  запрещена.
- Composition root вызывающего приложения создаёт adapters и явно передаёт их
  экземпляру SDK.
- Framework integrations располагаются в `apps/` или `examples/` и импортируют
  SDK только в направлении снаружи внутрь.
- Cyclic imports между слоями и service locator с неявным глобальным состоянием
  запрещены.

### Порты

Архитектура предусматривает отдельные ports как минимум для:

- определения формата и parser execution;
- database inspection и database load;
- candidate mapping и optional LLM provider;
- normalization, validation, business rules и security scanning;
- staging, audit и публикации событий.

Один adapter MAY реализовать несколько совместимых ports, но разделение прав и
trust boundaries от этого не меняется. В частности, inspection и write
connections остаются раздельными.

## Runtime и composition

### Async-first и sync facade

Canonical orchestration является асинхронной. Пошаговый async flow задаёт
семантику, а `analyze` и `ingest` являются thin orchestration поверх тех же
стадий и проверок. Sync facade — отдельная оболочка над async contract. Она MUST
завершаться контролируемой ошибкой при вызове внутри уже работающего event loop,
а не создавать вложенный loop или блокировать существующий.

### Instance registries

Каждый экземпляр SDK владеет своими registries и dependencies. Immutable default
registries создаются factory для каждого instance; global mutable registry
запрещён.

Database target registry содержит только trusted composition-owned immutable
policies. Каждая policy фиксирует endpoints, dialect, database/cluster identity,
maximum allowlist, denylist и `target_policy_fingerprint`. Per-run input
выбирает зарегистрированный target и MAY только сузить scope. Run snapshot
исключает регистрацию или расширение policy во время выполнения.

Регистрация parser, alias, rule или другого extension разрешена только когда у
instance нет активных runs. При старте каждый run получает immutable snapshot
registries и их версий. Попытка регистрации во время активного run завершается
контролируемой ошибкой. Между runs конфигурацию MAY изменить; изменение влияет
только на будущие runs. Поэтому параллельные runs не видят частично изменённую
конфигурацию из-за race condition.

Object registration принимает только trusted executable code, который
composition owner уже импортировал и создал в host process. Это относится к
parser, normalizer, validator, business rule, listener, DB и LLM adapters.
Untrusted parser регистрируется декларативным descriptor: module resolution,
import и constructor выполняет только `SandboxParserRunner`. Core получает
schema-validated output, но не загружает такой module в host. Для других типов
untrusted executable extensions sandbox protocol в MVP отсутствует, поэтому они
отклоняются. Declarative aliases, rules, templates, manifests и любой adapter
output остаются недоверенными данными.

В M3 это правило реализует `ParserRegistry`, который передаётся facade через
constructor dependency injection и доступен как `sdk.parsers`. Значение по
умолчанию создаётся отдельно для каждого facade; module-level mutable registry
отсутствует. Manual parser IDs образуют единое canonical namespace с
обнаруженными descriptors, а duplicate claim отклоняется без замены уже
зарегистрированного значения.

Selection выполняется только внутри async `registry.session()`, использует
frozen snapshot и последовательно вызывает trusted parsers в canonical ID
order. Сильные content signals (`internal_structure`, `signature` и
content-derived MIME) определяют совместимость формата. Затем применяются
`confidence`, `priority` и лексикографический `adapter_id`. Declared MIME и
extension не могут выбрать parser без подтверждения содержимым; их расхождение с
content evidence сохраняется как typed warning. Равносильное подтверждение
разных `format_id` завершается `PARSER_FORMAT_CONFLICT`, а не скрывается
tie-breaker.

Discovery entry points группы `structuraguard.parsers` запускается только явным
вызовом с allowlist policy. Оно создаёт bounded декларативные
`ParserPluginDescriptor`, но не вызывает `EntryPoint.load()`, не импортирует
module и не конструирует plugin. До появления `SandboxParserRunner` попытка
активации такого descriptor через `activate_plugin()` завершается
`SECURITY_SANDBOX_REQUIRED`.

Filesystem plugin metadata проходят отдельный FD-boundary: только фиксированные
файлы внутри native `.dist-info`, без symlink и special files, с byte caps и
проверкой подмены во время чтения. Неподдерживаемый metadata provider отклоняется
fail closed и не переключается на небounded stdlib getters.

### Инварианты импорта

`import structuraguard` MUST NOT:

- запускать server, event loop, background thread или устанавливать signal
  handlers;
- читать обязательные environment variables или secrets;
- выполнять network requests, открывать файлы или подключаться к БД;
- создавать schema, tables, staging resources или другие persistent objects;
- менять root logger или глобальную конфигурацию процесса.

Top-level exports остаются ленивыми: обычный `import structuraguard` не
загружает Pydantic, а `SDKConfig` и facade подгружаются при явном
обращении к символу. Сам core StructuraGuard не читает environment;
инициализация сторонней dependency начинается только за этой явной
границей.

Любой I/O начинается только после явного вызова операции. Resources и policies
передаются через constructors или параметры вызова.

## Целевой pipeline

После реализации соответствующих milestones полный ingest run должен проходить
следующие логические фазы. В M3 подтверждены DTO/port boundaries и parser
registry, но orchestration и concrete format adapters ещё отсутствуют.
Нормативный scope registry задан [разделом M3 технического задания][spec-m3].

1. Создание контекста run, безопасное чтение источника, проверка лимитов и
   вычисление source fingerprint.
2. Определение формата и technical parsing в `ExtractedBatch` без назначения
   бизнес-смысла.
3. Structural profiling, semantic analysis, создание и независимая проверка
   декларативного `ParsePlan`.
4. Детерминированное применение `ValidatedParsePlan` и построение
   `NormalizedBatch` с provenance; security checks применяются на границах.
5. Read-only inspection целевой БД, построение catalog, FK graph и database
   fingerprint.
6. Deterministic candidate generation и ranking; optional LLM mapping применяется
   только по разрешающей policy.
7. Создание декларативного `MappingPlan` и его независимая проверка.
8. Structural/type/business validation и разрешение FK.
9. Staging, повторная проверка fingerprints, target identity, audit capability
   и pre-commit invariants.
10. Транзакционная запись в основные таблицы и durable core audit/outbox record
   через DB adapter, затем commit либо rollback.
11. Формирование result/reports, post-commit delivery внешним listeners и cleanup
    принадлежащих SDK ресурсов.

`analyze`, `create_plan` и `dry_run` используют только применимые префиксы этого
pipeline. Они MUST NOT обходить те же detection, policy и plan-validation
границы. Канонические пользовательские сценарии определены в
[разделе 23 ТЗ][spec-public-api].

## Идентичность артефактов

### Source fingerprint

При первом чтении создаётся SHA-256 fingerprint точного содержимого источника.
Для программных объектов используется стабильное каноническое представление с
версией алгоритма. Persisted wire-форма едина: `sha256:<64 lowercase hex>`.
Bare digest на входной boundary нормализуется до неё до equality и duplicate
checks.

Path задаётся как `str` или `PathLike`, а raw text — явным `TextSource`, поэтому
SDK не определяет намерение caller по эвристике. Path transport доступен только
через immutable `SourcePathPolicy`, заданную trusted composition owner. Policy
ограничивает roots и не расширяется параметром run. SDK выполняет
descriptor-first safe open, не следует symlink, проверяет containment,
regular-file type, identity и limits после open, а fingerprint вычисляет по тому
же handle. Directory, device, FIFO, socket и symlink отклоняются.

Analysis создаёт `SourceAnalysis`: immutable analysis view и async-closeable
fingerprint-bound snapshot lease. Snapshot MAY использовать bounded temporary
storage, ссылку на caller-owned immutable source либо manifest для streaming
input; полное копирование в память не требуется. Lease передаёт ownership из
внутреннего `RunContext` caller и остаётся живым до `aclose()`, выхода из async
context либо expiry. `execute` использует только live lease; закрытый lease даёт
`SOURCE_SNAPSHOT_EXPIRED`. Для reuse plan caller создаёт новое analysis
исходного source, после чего fingerprints сравниваются. Незаметное повторное
чтение изменившегося path запрещено.

### Database fingerprint

Database fingerprint — стабильный SHA-256 от канонического разрешённого catalog:
schemas, tables, columns, types, PK, FK, unique и checks. Fingerprint строит
inspection adapter под read-only principal. Отдельный immutable
`target_identity` связывает dialect, database/cluster identity и
`target_policy_fingerprint` trusted composition owner без включения secret DSN.

Fingerprint, `target_identity` и `target_policy_fingerprint` фиксируются при
создании plan. Writer session до staging и commit доказывает, что относится к
тому же registered target и maximum policy, и повторяет применимую catalog
verification либо обеспечивает эквивалентную защиту транзакционной изоляцией и
locks. Schema drift приводит к `DATABASE_FINGERPRINT_MISMATCH`, а иной
endpoint/database — к `DATABASE_TARGET_MISMATCH`; до записи они запрещают
automatic commit, после начала transaction требуют rollback.

### Версионирование MappingPlan

`MappingPlan` является декларативным, immutable и не содержит SQL. Он связан как
минимум с source fingerprint, database fingerprint, `target_identity`,
`target_policy_fingerprint`, собственной version и validation evidence. Для
воспроизводимости также сохраняются версии SDK, parser, mapping algorithm,
policies, normalizers, validation rules и, если LLM использована,
provider/model и generation metadata.

Редактирование создаёт новую version, новый plan fingerprint и новую validation
evidence; изменение объекта in place запрещено. Перед execute повторно
проверяются plan identity, оба fingerprints и применимость evidence. Plan с
устаревшими или несовпадающими доказательствами не исполняется автоматически.

## Жизненный цикл run

Каждый публичный сценарий создаёт изолированный `RunContext` с уникальным
`run_id`, immutable configuration snapshot, resource budget и cancellation
scope. Mutable run data не разделяется между параллельными runs.

Обычно `RunContext` закрывает все принадлежащие SDK resources до возврата.
Исключение — успешный `inspect_source`: ownership bounded snapshot lease явно
передаётся возвращённому `SourceAnalysis`. Остальные resources inspection run
закрываются сразу, а snapshot — при `SourceAnalysis.aclose()`, expiry или отмене
до передачи. Combined `ingest` lease caller не передаёт и всегда закрывает сам.

### Активные состояния

Полный ingest использует следующий допустимый порядок:

```text
CREATED
  -> SOURCE_PROBING
  -> TECHNICAL_PARSING
  -> STRUCTURE_PROFILING
  -> STRUCTURE_ANALYZING
  -> PARSE_PLAN_CREATED
  -> PARSE_PLAN_VALIDATING
  -> SEMANTIC_PARSING
  -> NORMALIZED_DATA_PROFILING
  -> DATABASE_INSPECTING
  -> MAPPING
  -> MAPPING_PLAN_CREATED
  -> MAPPING_PLAN_VALIDATING
  -> NORMALIZING
  -> VALIDATING
  -> STAGING
  -> LOADING
  -> COMPLETED | COMPLETED_WITH_WARNINGS
```

Короткий сценарий MAY завершиться `COMPLETED` или
`COMPLETED_WITH_WARNINGS` на своей заявленной границе. Например, analysis-only
не входит в `STAGING` и `LOADING`. Пропуск prerequisite state или возврат из
terminal state в active state запрещён.

### Контролируемые исходы

- `NEEDS_REVIEW` завершает run при неоднозначном mapping, недостаточной
  confidence, validation conflict или schema drift до записи.
- `REJECTED_SECURITY` завершает run, когда security policy запрещает
  продолжение.
- `ROLLED_BACK` означает, что после открытия write transaction возникла
  критическая ошибка, изменения основных таблиц отменены и обязательное
  terminal audit evidence сохранено.
- `FAILED` используется для контролируемого сбоя до write transaction либо когда
  требуемая гарантия результата не может быть подтверждена.
- `CANCELLED` используется после обработки cancellation и обязательного cleanup.
  Если write transaction уже открыта, rollback предшествует этому terminal
  status и фиксируется событием `load.rolled_back`.

До commit configured timeout завершает run как `FAILED` с
`PROCESSING_TIMEOUT`, а явная caller cancellation — как `CANCELLED`. После
начала transaction оба пути сначала выполняют требуемый rollback и cleanup.
После подтверждённого commit timeout/cancellation не меняет data outcome и
приводит к `COMPLETED_WITH_WARNINGS` с post-commit details. Недопустимый state
transition отклоняется и регистрируется. Неподтверждённый transaction outcome
остаётся `FAILED` и явно отражается в report.

## Staging и запись

Staging — обязательная логическая граница между validated records и основными
таблицами. Реализация MAY использовать заранее подготовленную PostgreSQL schema,
внешний `StagingStore` или in-memory staging, если выбранный вариант обеспечивает
требуемые guarantees. Staging records разных runs изолируются по `run_id`.

Provisioning staging и transactional audit/outbox schema/tables выполняется
административно вне ingest. Pipeline и import-time code не создают эти objects.
Adapter обязан явно объявлять transaction, cleanup и retention capabilities;
внешний store, который не может участвовать в DB transaction, не должен
ослаблять атомарность основных таблиц.

### Transaction boundary

`atomic` является load error policy по умолчанию. Все записи одного run в
основные таблицы выполняются в одной transaction; statements MAY отправляться
batches внутри неё. Любая record-level или критическая ошибка исключает commit
либо приводит к полному rollback основных таблиц.

`safety_policy="auto_safe"` сначала применяет run-level gates: parser/format,
plan, mapping confidence, fingerprints, registered target identity/policy,
allow/denylist, DDL, security, transaction и audit durability. Они всегда veto.
Только после этого
`quarantine_invalid` MAY изолировать record-local validation failures, а
`best_effort` MAY пропускать явно recoverable record-level failures. Обе policy
требуют opt-in; partial commit завершается `COMPLETED_WITH_WARNINGS`.
`error_policy` независим от `safety_policy` и `dry_run`, но не ослабляет их.

Для реального load durable core audit/outbox record включается в transaction
target mutations. Transactional capability и доступность проверяются до
`LOADING`; отсутствие даёт `AUDIT_DURABILITY_REQUIRED` без data-plane mutation.
External audit stores/listeners получают post-commit delivery из outbox. Их сбой
не откатывает уже committed data, сохраняет retry evidence и MAY привести к
`COMPLETED_WITH_WARNINGS`.

Невозможность подтвердить commit/rollback считается ошибкой с неопределённым
результатом, а не успешным завершением. Audit evidence MUST позволять отличить
committed, rolled-back и unknown outcome без сохранения restricted raw values.

### Dry run

`dry_run` MUST NOT изменять source storage/content, основные таблицы или
persistent staging. Caller-owned one-shot stream/iterator после чтения остаётся
потреблённым согласно public ownership contract. Разрешено только transient
in-memory staging либо staging внутри transaction, которая гарантированно
завершается rollback и cleanup. Adapter без такой гарантии не совместим с
`dry_run`.

События и callbacks MAY передаваться явно подключённым caller-owned ports. Их
внешние side effects контролирует caller и они не дают SDK права изменять
data plane. Отчёт dry run помечает предполагаемые операции и не представляется
как evidence состоявшегося commit.

## Граница базы данных

### Support tiers

- PostgreSQL — единственная MUST-supported production target для MVP и основной
  интеграционный стенд.
- SQLite — test-only target для unit/integration tests. Его поведение не является
  обещанием production portability или эквивалентности PostgreSQL.
- Другие СУБД MAY подключаться через adapters после собственных capability и
  contract tests; до этого они не считаются поддерживаемыми targets.

### Разделение principals

Обычный pipeline использует разные credentials и connections:

- inspection principal имеет read-only доступ только к разрешённым metadata;
- writer principal имеет только необходимые `SELECT`, `INSERT` и `UPDATE` для
  allowlisted staging, target и transactional audit/outbox tables;
- migration/admin principal отсутствует в runtime composition обычного run.

Один привилегированный principal для обеих ролей запрещён. DSN, credentials и
секреты не входят в domain objects, prompts, logs, errors, reports или audit.
Table/schema/column identifiers происходят только из проверенного catalog и
allowlist, а values передаются параметризованно. SQL формирует исключительно DB
adapter.

Target denylist имеет приоритет над include/allowlist и не расширяется per-run
input. `pg_catalog`, `information_schema`, `pg_toast*`, `pg_temp_*` и
SDK-owned internal objects не могут быть mapping/load targets. Pre-provisioned
staging и audit/outbox доступны только dedicated ports; inspector MAY читать
необходимые system catalogs как metadata. Обе connections должны подтвердить
один `target_identity` и зарегистрированный `target_policy_fingerprint`;
одинаковая schema в разных databases не считается совпадением. Per-run selectors
пересекаются с maximum allowlist и не могут его расширить.

### DDL deny-by-default

Обычный pipeline запрещает `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `GRANT` и
`REVOKE`. Запрет действует также при инициализации SDK, staging и dry run. Если
target structure отсутствует, SDK MAY вернуть только декларативное schema
proposal для ручного рассмотрения. Применение DDL и административный apply API
не входят в MVP.

## Граница LLM

Deterministic mapping работает без LLM и всегда предшествует optional LLM
adapter. Модель получает только разрешённый policy минимальный context и
ограниченный набор candidates. Она не получает DB connection, credentials,
tools или право выполнять code/commands.

LLM output считается недоверенным предложением. Он проходит schema validation и
независимую `MappingPlan` validation. LLM не создаёт исполняемый SQL, не выбирает
объекты вне allowlist, не объявляет uniqueness и не обходит DDL/security policy.
Privacy routing, prompt-injection controls и parser isolation канонически
описаны в [модели угроз](threat-model.md).

## Наблюдаемость и cleanup

Pipeline публикует типизированные events через caller-provided port. SDK не
настраивает logging backend и не создаёт встроенное глобальное audit storage.
Events содержат `run_id`, безопасные identifiers, decisions и hashes, но не DSN,
credentials, secrets или restricted raw values.

Core audit record для реального commit сохраняется атомарно через
`TransactionalAuditPort` либо эквивалентный outbox DB adapter. Caller-provided
listener не заменяет эту гарантию. Terminal rollback/failure event сохраняется
отдельно после rollback; если это не удалось, result явно содержит audit gap и
завершается `FAILED`, хотя `LoadReport` сохраняет подтверждённый transaction
outcome `rolled_back`.

Resources закрываются в обратном порядке владения при success, failure,
cancellation и timeout. SDK закрывает только созданные им streams, connections и
transactions, а также temporary snapshots, ownership которых не передан live
`SourceAnalysis`; caller-owned resource lifecycle остаётся за caller. Канонические
требования к cancellation, timeout и cleanup задаёт [NFR-010 ТЗ][spec-nfr-010].
Ошибка listener или cleanup не может превратить
отклонённый plan в разрешённую загрузку и не скрывает неизвестный transaction
outcome.

## Журнал решений M0

Все решения ниже имеют статус `ACCEPTED_M0`. Междокументные решения закреплены в
[ADR 0001](adr/0001-public-api-and-run-policies.md) и
[ADR 0002](adr/0002-security-boundary-defaults.md). При последующем изменении
долгоживущего решения потребуется новый ADR.

- `D-01`: canonical stepwise async flow; `analyze` и `ingest` — convenience
  orchestration с той же семантикой. См. [ADR 0001].
- `D-02`: source type задаётся однозначно; path ограничивает
  `SourcePathPolicy`, а `SourceAnalysis` владеет fingerprint-bound snapshot
  lease. См. [ADR 0001].
- `D-03`: `safety_policy`, `error_policy` и `dry_run` — независимые параметры.
  См. [ADR 0001].
- `D-04`: dry run не оставляет persistent data-plane mutations; допускается
  только rollback-only или in-memory staging с cleanup. См. [ADR 0001].
- `D-05`: `MappingPlan` immutable и versioned; execute повторно проверяет plan,
  source/database/target identity и policy fingerprints, validation evidence.
  См. [ADR 0001].
- `D-06`, `D-07`, `D-09`: LLM routing, parser isolation, transactional audit
  и key/retention ports приняты в [модели угроз](threat-model.md) и [ADR 0002].
- `D-08`: PostgreSQL поддерживается в production, SQLite используется только в
  тестах, остальные СУБД являются extension adapters.
- `D-10`: MVP формирует schema proposal, но не применяет DDL.
- `D-11`: registries принадлежат instance; каждый run фиксирует immutable
  snapshot, а регистрация между runs разрешена при отсутствии активных runs.
  См. [ADR 0001].
- `D-12`: resource policies имеют безопасные defaults и абсолютные caps для
  strict mode; численные limits принадлежат
  [требованиям](requirements.md) и [модели угроз](threat-model.md). См.
  [ADR 0002].
- `D-13`: technical parser возвращает только physical `ExtractedBatch`;
  `NormalizedBatch` создаётся после independent validation и применения
  `ParsePlan`. `MappingPlan` использует только semantic/catalog references. См.
  [ADR 0003].

[ADR 0001]: adr/0001-public-api-and-run-policies.md
[ADR 0002]: adr/0002-security-boundary-defaults.md
[ADR 0003]: adr/0003-two-stage-parsing-contracts.md
[spec-m2]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m2-доменные-модели-и-contracts
[spec-m3]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#m3-parser-registry
[spec-nfr-010]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#nfr-010-отмена-и-timeout
[spec-public-api]: https://github.com/NevermoreKatana/structuraguard/blob/main/StructuraGuard_SDK_Technical_Specification.md#23-публичный-api-sdk

## Вне архитектурного scope M0–M3

M0 не определял точные DTO, package layout, SQL schema staging или выбор
библиотек adapters. M1 зафиксировал package scaffold. M2 фиксирует DTO,
fingerprint/serialization rules и adapter protocols. M3 реализует parser
registry и descriptor-only plugin discovery, но не реализует pipeline, format
parsers, analyzers/executors, DB reflection/load, LLM providers или sandbox
runner.
Численные resource limits канонически задаются в
[требованиях](requirements.md) и не дублируются здесь. M0 не включает web
deployment, worker, UI, OCR/media processing, administrative migrations и
production support других СУБД. Полный out-of-scope и acceptance traceability
также определены в требованиях.
